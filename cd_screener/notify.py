"""Windows toast notifications (the pop-ups in the bottom-right corner).

Uses the built-in Windows notification API through PowerShell, so no extra packages are
needed. On other systems, or if anything goes wrong, it quietly does nothing: a missing
notification must never break a price run.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# Show toasts under Windows PowerShell's app identity, which is registered on every PC.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$title = [Security.SecurityElement]::Escape($env:CD_TOAST_TITLE)
$body = [Security.SecurityElement]::Escape($env:CD_TOAST_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CD_TOAST_APP).Show($toast)
"""


def toast(title: str, body: str) -> bool:
    """Show a Windows notification. Returns True if it was handed to Windows."""
    if sys.platform != "win32":
        return False
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, "CD_TOAST_TITLE": title, "CD_TOAST_BODY": body, "CD_TOAST_APP": _APP_ID}
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=env, timeout=30, check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Could not show a notification: %s", exc)
        return False
