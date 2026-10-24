"""Blocking FTP/SFTP client intended for a background worker, never the UI thread."""
from __future__ import annotations

import asyncio
import ftplib
import os
import posixpath
import socket
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .transport import UnknownHostKey, ChangedHostKey, connect_ssh


class TransferCancelled(RuntimeError):
    pass


class _FTP(ftplib.FTP):
    data_socket = None

    def ntransfercmd(self, command, rest=None):
        connection, size = super().ntransfercmd(command, rest)
        self.data_socket = connection
        return connection, size


class TransferClient:
    def __init__(self, host, known_hosts_path):
        self.host, self.known_hosts_path = host, Path(known_hosts_path)
        self._ftp = self._ssh = self._sftp = self._loop = None
        self.timeout = 120
        self._active_task = None
        self._cancelled = False
        self._cancel_tasks = []

    def __enter__(self):
        self._cancelled = False
        try:
            if self.host.protocol.lower() == "ftp":
                self._ftp = _FTP(timeout=30)
                self._ftp.connect(self.host.address, int(self.host.port))
                self._ftp.login(self.host.username or "anonymous", self.host.password or "")
                self._ftp.set_pasv(True)
            elif self.host.protocol.lower() in ("sftp", "ssh"):
                self._loop = asyncio.new_event_loop()
                self._ssh = self._run(connect_ssh(self.host, self.known_hosts_path))
                self._sftp = self._run(self._ssh.start_sftp_client())
            else:
                raise ValueError("File transfers support FTP and SFTP")
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        if self._cancelled and exc_type is not None:
            raise TransferCancelled("Transfer cancelled") from None

    def _run(self, awaitable):
        async def operation():
            self._active_task = asyncio.current_task()
            work = asyncio.ensure_future(awaitable)
            try:
                return await asyncio.wait_for(asyncio.shield(work), timeout=self.timeout)
            except TimeoutError:
                self._cancel_tasks.extend(asyncio.all_tasks())
                if self._ssh:
                    self._ssh.abort()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise TimeoutError(f"SFTP operation timed out after {self.timeout:g} seconds") from None
            except asyncio.CancelledError:
                # A shielded operation can still own local files. Finish its
                # cancellation before the caller attempts to remove a partial.
                if self._ssh:
                    self._ssh.abort()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise TransferCancelled("Transfer cancelled") from None
            except Exception:
                if self._cancelled:
                    raise TransferCancelled("Transfer cancelled") from None
                raise
            finally:
                self._active_task = None
        return self._loop.run_until_complete(operation())

    def cancel(self):
        """May be called from the UI thread while a background operation runs."""
        if self._cancelled:
            return
        self._cancelled = True
        ftp = self._ftp
        if ftp:
            for connection in (ftp.data_socket, ftp.sock):
                if connection:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        loop, task = self._loop, self._active_task
        if loop and loop.is_running() and task:
            def abort():
                self._cancel_tasks = list(asyncio.all_tasks(loop))
                # Interrupt remote file cleanup as well as the transfer coroutine.
                if self._ssh:
                    self._ssh.abort()
                task.cancel()
            try:
                loop.call_soon_threadsafe(abort)
            except RuntimeError:
                pass

    def listdir(self, path="."):
        entries = []
        if self._ftp:
            # MLSD provides unambiguous names and metadata, unlike LIST output.
            try:
                listing = list(self._ftp.mlsd(path))
            except ftplib.error_perm as exc:
                if str(exc)[:3] not in ("500", "501", "502", "504"):
                    raise
                listing = self._legacy_ftp_listing(path)
            for name, facts in listing:
                kind = facts.get("type", "")
                if name in (".", "..") or kind in ("cdir", "pdir"):
                    continue
                modified = None
                if facts.get("modify"):
                    try:
                        modified = datetime.strptime(facts["modify"].split(".")[0], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        pass
                entries.append(dict(name=name, is_dir=kind == "dir", size=int(facts.get("size", 0)), modified=modified))
        elif self._sftp:
            for entry in self._run(self._sftp.readdir(path)):
                if entry.filename in (".", ".."):
                    continue
                attrs = entry.attrs
                entries.append(dict(name=entry.filename,
                                    is_dir=stat.S_ISDIR(attrs.permissions or 0),
                                    size=attrs.size or 0, modified=attrs.mtime))
        else:
            raise RuntimeError("Transfer client is not connected")
        return sorted(entries, key=lambda item: (not item["is_dir"], item["name"].casefold()))

    def _legacy_ftp_listing(self, path):
        """Older devices lack MLSD. Probe NLST entries without parsing LIST prose."""
        previous = self._ftp.pwd()
        listing = []
        try:
            self._ftp.cwd(path)
            directory = self._ftp.pwd()
            for raw_name in self._ftp.nlst():
                name = posixpath.basename(raw_name.rstrip("/"))
                if name in (".", "..", ""):
                    continue
                facts = {"type": "file"}
                try:
                    self._ftp.cwd(name)
                except ftplib.error_perm:
                    self._ftp.voidcmd("TYPE I")
                    try:
                        facts["size"] = self._ftp.size(name) or 0
                    except ftplib.error_perm:
                        facts["size"] = 0
                else:
                    facts["type"] = "dir"
                    self._ftp.cwd(directory)
                listing.append((name, facts))
        finally:
            self._ftp.cwd(previous)
        return listing

    def upload(self, local, remote, progress=None):
        local = Path(local)
        total, done = local.stat().st_size, 0
        if self._ftp:
            def report(block):
                nonlocal done
                done += len(block)
                if progress:
                    progress(done, total)
            with local.open("rb") as file:
                self._ftp.storbinary(f"STOR {remote}", file, blocksize=65536, callback=report)
        else:
            self._run(self._sftp.put(str(local), remote,
                      progress_handler=(lambda _a, _b, done, total: progress(done, total)) if progress else None))

    def download(self, remote, local, progress=None):
        # A failed transfer must not destroy a previous complete local file.
        local = Path(local)
        fd, partial = tempfile.mkstemp(prefix=f".{local.name}.", suffix=".part", dir=local.parent)
        os.close(fd)
        try:
            if self._ftp:
                self._ftp.voidcmd("TYPE I")
                try:
                    total = self._ftp.size(remote) or 0
                except ftplib.error_perm:
                    total = 0
                done = 0
                with open(partial, "wb") as file:
                    def receive(block):
                        nonlocal done
                        file.write(block)
                        done += len(block)
                        if progress:
                            progress(done, total)
                    self._ftp.retrbinary(f"RETR {remote}", receive, blocksize=65536)
            else:
                # Own the local handle here. AsyncSSH get() can skip closing its
                # destination when remote source cleanup raises after an abort.
                with open(partial, "wb") as file:
                    self._run(self._download_sftp(remote, file, progress))
            os.replace(partial, local)
        finally:
            if os.path.exists(partial):
                os.unlink(partial)

    async def _download_sftp(self, remote, destination, progress):
        async with self._sftp.open(remote, "rb") as source:
            total = (await source.stat()).size or 0
            done = 0
            if progress:
                progress(0, total)
            while True:
                block = await source.read(262144)
                if not block:
                    break
                destination.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
            if done != total:
                raise OSError("Remote file size changed during download")

    def mkdir(self, path):
        if self._ftp:
            self._ftp.mkd(path)
        else:
            self._run(self._sftp.mkdir(path))

    def delete(self, path, is_dir=False):
        if self._ftp:
            (self._ftp.rmd if is_dir else self._ftp.delete)(path)
        else:
            self._run((self._sftp.rmdir if is_dir else self._sftp.remove)(path))

    def close(self):
        if self._ftp:
            try:
                self._ftp.close()
            finally:
                self._ftp = None
        if self._ssh:
            self._ssh.close()
            try:
                self._run(self._ssh.wait_closed())
            finally:
                self._ssh = self._sftp = None
        if self._loop:
            tasks = set(self._cancel_tasks) | asyncio.all_tasks(self._loop)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._cancel_tasks.clear()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None
