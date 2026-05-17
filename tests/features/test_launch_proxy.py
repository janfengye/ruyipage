# -*- coding: utf-8 -*-

from unittest import mock

from ruyipage import FirefoxOptions, launch


def test_quick_start_sets_proxy():
    opts = FirefoxOptions()

    opts.quick_start(proxy="http://127.0.0.1:7890")

    assert opts.proxy == "http://127.0.0.1:7890"


def test_launch_forwards_proxy_to_options():
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
        launch(proxy="http://127.0.0.1:7890")

    opts = created_opts["opts"]
    assert opts.proxy == "http://127.0.0.1:7890"


def test_launch_forwards_user_dir_to_options(tmp_path):
    created_opts = {}

    def fake_page(opts):
        created_opts["opts"] = opts
        return object()

    with mock.patch("ruyipage.FirefoxPage", side_effect=fake_page):
        launch(user_dir=str(tmp_path))

    opts = created_opts["opts"]
    assert opts.profile_path == str(tmp_path)


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
