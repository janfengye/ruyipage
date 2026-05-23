# -*- coding: utf-8 -*-

from unittest import mock

from ruyipage import FirefoxOptions, launch


def test_quick_start_sets_proxy():
    opts = FirefoxOptions()

    opts.quick_start(proxy="http://127.0.0.1:7890")

    assert opts.proxy == "http://127.0.0.1:7890"


def test_quick_start_sets_fpfile():
    opts = FirefoxOptions()

    opts.quick_start(fpfile=r"C:\firefox\fp.txt")

    assert opts.fpfile == r"C:\firefox\fp.txt"


def test_launch_forwards_proxy_to_options():
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
        launch(proxy="http://127.0.0.1:7890")

    opts = created_opts["opts"]
    assert opts.proxy == "http://127.0.0.1:7890"


def test_launch_forwards_fpfile_to_options():
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
        launch(fpfile=r"C:\firefox\fp.txt")

    opts = created_opts["opts"]
    assert opts.fpfile == r"C:\firefox\fp.txt"


def test_launch_forwards_user_dir_to_options(tmp_path):
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
        launch(user_dir=str(tmp_path))

    opts = created_opts["opts"]
    assert opts.profile_path == str(tmp_path)


def test_write_prefs_uses_socks5_proxy_from_fpfile(tmp_path):
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text(
        "proxy.example.com:1000:username-value:password-value\n",
        encoding="utf-8",
    )

    opts = FirefoxOptions()
    opts.quick_start(user_dir=str(tmp_path), fpfile=str(fpfile))

    opts.write_prefs_to_profile()

    content = (tmp_path / "user.js").read_text(encoding="utf-8")
    assert 'user_pref("network.proxy.type", 1);' in content
    assert 'user_pref("network.proxy.socks", "proxy.example.com");' in content
    assert 'user_pref("network.proxy.socks_port", 1000);' in content
    assert 'user_pref("network.proxy.socks_version", 5);' in content
    assert 'user_pref("network.proxy.socks_remote_dns", true);' in content
    assert "username-value" not in content
    assert "password-value" not in content


def test_write_prefs_uses_socksauth_fields_from_fpfile(tmp_path):
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text(
        "\n".join(
            [
                "socksauth.host:proxy.example.com",
                "socksauth.port:1000",
                "socksauth.username:username-value",
                "socksauth.password:password-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    opts = FirefoxOptions()
    opts.quick_start(user_dir=str(tmp_path), fpfile=str(fpfile))

    opts.write_prefs_to_profile()

    content = (tmp_path / "user.js").read_text(encoding="utf-8")
    assert 'user_pref("network.proxy.type", 1);' in content
    assert 'user_pref("network.proxy.socks", "proxy.example.com");' in content
    assert 'user_pref("network.proxy.socks_port", 1000);' in content
    assert 'user_pref("network.proxy.socks_version", 5);' in content
    assert 'user_pref("network.proxy.socks_remote_dns", true);' in content
    assert "username-value" not in content
    assert "password-value" not in content


def test_write_prefs_does_not_treat_fpfile_ipv6_as_socks5_proxy(tmp_path):
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text(
        "\n".join(
            [
                "webdriver:0",
                "local_webrtc_ipv4:203.0.113.45",
                "local_webrtc_ipv6:2001:db8::1",
                "public_webrtc_ipv6:2001:db8::1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    opts = FirefoxOptions()
    opts.quick_start(user_dir=str(tmp_path), fpfile=str(fpfile))

    opts.write_prefs_to_profile()

    content = (tmp_path / "user.js").read_text(encoding="utf-8")
    assert "network.proxy.socks" not in content
    assert "network.proxy.socks_port" not in content


def test_build_command_includes_profile_and_fpfile():
    opts = FirefoxOptions()
    opts.set_browser_path(r"C:\firefox\firefox.exe")
    opts.set_user_dir(r"C:\firefox\socks5-profile")
    opts.set_fpfile(r"C:\firefox\fp.txt")

    cmd = opts.build_command()

    assert "--no-remote" in cmd
    assert "--profile" in cmd
    assert r"C:\firefox\socks5-profile" in cmd
    assert r"--fpfile=C:\firefox\fp.txt" in cmd


def test_launch_prefers_resolved_runtime_when_no_browser_path():
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.resolve_firefox_path", return_value="D:/runtime/firefox.exe"):
        with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
            launch()

    opts = created_opts["opts"]
    assert opts.browser_path == "D:/runtime/firefox.exe"
