"""Ciena SAOS support."""
import time
import re
import os
from netmiko.base_connection import BaseConnection
from netmiko.scp_handler import BaseFileTransfer


class CienaSaosBase(BaseConnection):
    """
    Ciena SAOS support.

    Implements methods for interacting Ciena Saos devices.

    Disables enable(), check_enable_mode(), config_mode() and
    check_config_mode()
    """

    def session_preparation(self):
        self._test_channel_read()
        self.set_base_prompt()
        self.disable_paging(command="system shell session set more off")
        # Clear the read buffer
        time.sleep(0.3 * self.global_delay_factor)
        self.clear_buffer()

    def _enter_shell(self):
        """Enter the Bourne Shell."""
        return self.send_command("diag shell", expect_string=r"[$#]")

    def _return_cli(self):
        """Return to the Ciena SAOS CLI."""
        return self.send_command("exit", expect_string=r"[>]")

    def check_enable_mode(self, *args, **kwargs):
        """No enable mode on Ciena SAOS."""
        return True

    def enable(self, *args, **kwargs):
        """No enable mode on Ciena SAOS."""
        return ""

    def exit_enable_mode(self, *args, **kwargs):
        """No enable mode on Ciena SAOS."""
        return ""

    def check_config_mode(self, check_string=">", pattern=""):
        """No config mode on Ciena SAOS."""
        return False

    def config_mode(self, config_command=""):
        """No config mode on Ciena SAOS."""
        return ""

    def exit_config_mode(self, exit_config=""):
        """No config mode on Ciena SAOS."""
        return ""

    def save_config(self, cmd="configuration save", confirm=False, confirm_response=""):
        """Saves Config."""
        return self.send_command(command_string=cmd)


class CienaSaosSSH(CienaSaosBase):
    pass


class CienaSaosTelnet(CienaSaosBase):
    def __init__(self, *args, **kwargs):
        default_enter = kwargs.get("default_enter")
        kwargs["default_enter"] = "\r\n" if default_enter is None else default_enter
        super().__init__(*args, **kwargs)


class CienaSaosFileTransfer(BaseFileTransfer):
    """Ciena SAOS SCP File Transfer driver."""

    def __init__(
        self, ssh_conn, source_file, dest_file, file_system="", direction="put"
    ):
        if file_system == "":
            file_system = f"/tmp/users/{ssh_conn.username}"
        return super().__init__(
            ssh_conn=ssh_conn,
            source_file=source_file,
            dest_file=dest_file,
            file_system=file_system,
            direction=direction,
        )

    def remote_space_available(self, search_pattern=""):
        """Return space available on Ciena SAOS"""
        remote_cmd = "file vols"
        remote_output = self.ssh_ctl_chan.send_command_expect(remote_cmd)

        # Try to ensure parsing is correct:
        # Filesystem           1K-blocks      Used Available Use% Mounted on
        # var                       1024       528       496  52% /var
        remote_output = remote_output.strip()

        # First line is the header; rest are the actual file system info
        header_line, *filesystem_lines = remote_output.splitlines()

        filesystem, _, _, space_avail, *_ = header_line.split()
        if "Filesystem" != filesystem or "Avail" not in space_avail:
            # Filesystem  1K-blocks  Used   Avail Capacity  Mounted on
            msg = (
                f"Parsing error, unexpected output from {remote_cmd}:\n{remote_output}"
            )
            raise ValueError(msg)

        # longest_match keeps track of the most specific match of self.file_system
        longest_match = {"file_system": None, "match_length": 0, "space_available": ""}

        for filesystem_line in filesystem_lines:
            filesystem, _, _, space_avail, _, mounted_on = filesystem_line.split()
            if (
                self.file_system.startswith(mounted_on)
                and len(mounted_on) > longest_match["match_length"]
            ):
                longest_match = {
                    "file_system": filesystem,
                    "match_length": len(mounted_on),
                    "space_available": space_avail,
                }

        space_available = longest_match["space_available"]
        if not re.search(r"^\d+$", space_available):
            msg = (
                f"Parsing error, unexpected output from {remote_cmd}:\n{remote_output}"
            )
            raise ValueError(msg)

        return int(space_available) * 1024

    def check_file_exists(self, remote_cmd=""):
        """Check if the dest_file already exists on the file system (return boolean)."""
        if self.direction == "put":
            if not remote_cmd:
                remote_cmd = f"file ls {self.file_system}/{self.dest_file}"
            remote_out = self.ssh_ctl_chan.send_command_expect(remote_cmd)
            search_string = re.escape(f"{self.file_system}/{self.dest_file}")
            if "ERROR" in remote_out:
                return False
            elif re.search(search_string, remote_out):
                return True
            else:
                raise ValueError("Unexpected output from check_file_exists")
        elif self.direction == "get":
            return os.path.exists(self.dest_file)

    def remote_file_size(self, remote_cmd="", remote_file=None):
        """Get the file size of the remote file."""
        if remote_file is None:
            if self.direction == "put":
                remote_file = self.dest_file
            elif self.direction == "get":
                remote_file = self.source_file

        remote_file = f"{self.file_system}/{remote_file}"

        if not remote_cmd:
            remote_cmd = f"file ls -l {remote_file}"

        remote_out = self.ssh_ctl_chan.send_command_expect(remote_cmd)

        if "No such file or directory" in remote_out:
            raise IOError("Unable to find file on remote system")

        escape_file_name = re.escape(remote_file)
        pattern = r"^.* ({}).*$".format(escape_file_name)
        match = re.search(pattern, remote_out, flags=re.M)
        if match:
            # Format: -rw-r--r--  1 pyclass  wheel  12 Nov  5 19:07 /var/tmp/test3.txt
            line = match.group(0)
            file_size = line.split()[4]
            return int(file_size)

        raise ValueError(
            "Search pattern not found for remote file size during SCP transfer."
        )

    def remote_md5(self, base_cmd="", remote_file=None):
        """Calculate remote MD5 and returns the hash.

        This command can be CPU intensive on the remote device.
        """
        if base_cmd == "":
            base_cmd = "md5sum"
        if remote_file is None:
            if self.direction == "put":
                remote_file = self.dest_file
            elif self.direction == "get":
                remote_file = self.source_file

        remote_md5_cmd = f"{base_cmd} {self.file_system}/{remote_file}"

        self.ssh_ctl_chan._enter_shell()
        dest_md5 = self.ssh_ctl_chan.send_command(remote_md5_cmd, expect_string=r"[$#]")
        self.ssh_ctl_chan._return_cli()
        dest_md5 = self.process_md5(dest_md5, pattern=r"([0-9a-f]+)\s+")
        return dest_md5

    def enable_scp(self, cmd="system server scp enable"):
        return super().enable_scp(cmd=cmd)

    def disable_scp(self, cmd="system server scp disable"):
        return super().disable_scp(cmd=cmd)
