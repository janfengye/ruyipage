# -*- coding: utf-8 -*-
"""Static Firefox runtime manifest for ruyiPage managed Firefox."""

RELEASE_TAG = "v1.2.66"
FIREFOX_VERSION = "155.0"
RELEASE_URL_TEMPLATE = "https://github.com/LoseNine/ruyipage/releases/download/{}"
RELEASE_BASE_URL = RELEASE_URL_TEMPLATE.format(RELEASE_TAG)

# Windows 和 Linux 的内核构建不一定同时完成，因此每个平台各自记录版本与
# release tag，未跟上的平台继续指向仍然可用的旧 release，而不是指向一个
# 尚未上传资源的 tag 直接 404。
PREVIOUS_RELEASE_TAG = "v1.2.58"
PREVIOUS_FIREFOX_VERSION = "155.0a1"

RUNTIME_NAME = "firefox"

RUNTIMES = {
    "win64": {
        "name": RUNTIME_NAME,
        "version": FIREFOX_VERSION,
        "release": RELEASE_TAG,
        "asset": "firefox-155.0.en-US.win64-20260829.zip",
        "archive_type": "zip",
        "executable": "firefox/firefox.exe",
        "install_subdir": "firefox-155.0-v1.2.66-win64",
        "max_files": 20000,
        "max_total_size": 900 * 1024 * 1024,
    },
    "linux-x86_64": {
        # Linux 的 155.0 构建尚未完成，暂时继续使用 v1.2.58 的 155.0a1 包。
        "name": RUNTIME_NAME,
        "version": PREVIOUS_FIREFOX_VERSION,
        "release": PREVIOUS_RELEASE_TAG,
        "asset": "firefox-155.0a1.en-US.linux-x86_64.tar.xz",
        "archive_type": "tar.xz",
        "executable": "firefox/firefox",
        "install_subdir": "firefox-155.0a1-v1.2.58-linux-x86_64",
        "max_files": 20000,
        "max_total_size": 900 * 1024 * 1024,
    },
}


def runtime_url(info, base_url=None):
    """Return the download URL for a runtime info entry."""
    if base_url:
        root = base_url.rstrip("/")
    else:
        root = RELEASE_URL_TEMPLATE.format(info.get("release") or RELEASE_TAG)
    return "{}/{}".format(root.rstrip("/"), info["asset"])
