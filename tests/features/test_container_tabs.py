# -*- coding: utf-8 -*-

from unittest import mock

from ruyipage import FirefoxOptions, FirefoxPage


def test_page_new_tab_forwards_user_context():
    page = FirefoxPage.__new__(FirefoxPage)
    fake_browser = mock.Mock()
    page._firefox = fake_browser

    page.new_tab("https://example.com", background=True, user_context="ctx-1")

    fake_browser.new_tab.assert_called_once_with(
        "https://example.com", True, user_context="ctx-1"
    )


def test_page_new_container_tab_forwards_to_browser():
    page = FirefoxPage.__new__(FirefoxPage)
    fake_browser = mock.Mock()
    page._firefox = fake_browser

    page.new_container_tab("https://example.com", background=True)

    fake_browser.new_container_tab.assert_called_once_with(
        url="https://example.com", background=True
    )


def test_page_new_container_tabs_forwards_to_browser():
    page = FirefoxPage.__new__(FirefoxPage)
    fake_browser = mock.Mock()
    page._firefox = fake_browser

    page.new_container_tabs(5, "https://example.com", background=False)

    fake_browser.new_container_tabs.assert_called_once_with(
        count=5, url="https://example.com", background=False
    )


def test_browser_new_tab_includes_user_context():
    from ruyipage._base.browser import Firefox

    browser = Firefox.__new__(Firefox)
    browser._context_ids = ["root-ctx"]
    browser._contexts = {}
    browser._driver = mock.Mock()
    browser._driver.run.return_value = {"context": "ctx-2"}
    browser._get_or_create_tab = mock.Mock(return_value=mock.Mock())

    browser.new_tab(user_context="uc-1")

    browser._driver.run.assert_called_once_with(
        "browsingContext.create",
        {
            "type": "tab",
            "background": False,
            "referenceContext": "root-ctx",
            "userContext": "uc-1",
        },
    )


def test_browser_new_container_tab_creates_user_context_then_tab():
    from ruyipage._base.browser import Firefox

    browser = Firefox.__new__(Firefox)
    browser._driver = mock.Mock()
    browser.new_tab = mock.Mock(return_value="tab-1")
    browser._driver.run.return_value = {"userContext": "uc-2"}

    result = browser.new_container_tab(url="https://example.com", background=True)

    assert result == "tab-1"
    browser._driver.run.assert_called_once_with("browser.createUserContext")
    browser.new_tab.assert_called_once_with(
        url="https://example.com", background=True, user_context="uc-2"
    )


def test_browser_new_container_tabs_with_count_one_uses_bidi_path():
    from ruyipage._base.browser import Firefox

    browser = Firefox.__new__(Firefox)
    browser.new_container_tab = mock.Mock(return_value="tab-1")

    tabs = browser.new_container_tabs(1, url="https://example.com", background=False)

    assert tabs == ["tab-1"]
    browser.new_container_tab.assert_called_once_with(
        url="https://example.com", background=False
    )


def test_set_per_tab_proxies_normalizes_lines_and_generates_runtime_fpfile(tmp_path):
    opts = FirefoxOptions()
    opts.set_user_dir(str(tmp_path))
    opts.set_per_tab_proxies(
        [
            "proxy.example.com:1000:user-a:pass-a",
            "socks5://proxy2.example.com:1001:user-b:pass-b",
        ],
        exhausted="wrap",
    )

    opts.prepare_runtime_files()

    assert opts.fpfile.endswith("ruyipage_per_tab_proxy_fp.txt")
    content = (tmp_path / "ruyipage_per_tab_proxy_fp.txt").read_text(encoding="utf-8")
    assert "proxy.rotate.enabled=true" in content
    assert "proxy.rotate.exhausted=wrap" in content
    assert "proxy.rotate.proxy=socks5://proxy.example.com:1000:user-a:pass-a" in content
    assert "proxy.rotate.proxy=socks5://proxy2.example.com:1001:user-b:pass-b" in content


def test_prepare_runtime_files_merges_source_fpfile_and_omits_old_rotate_lines(tmp_path):
    source = tmp_path / "source_fp.txt"
    source.write_text(
        "\n".join(
            [
                "webdriver:0",
                "proxy.rotate.enabled=false",
                "proxy.rotate.exhausted=stop",
                "proxy.rotate.proxy=socks5://old.example.com:9000:old:secret",
                "canvas:123",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    opts = FirefoxOptions()
    opts.set_user_dir(str(tmp_path))
    opts.set_fpfile(str(source))
    opts.set_per_tab_proxies(["proxy.example.com:1000:user-a:pass-a"])

    opts.prepare_runtime_files()

    content = (tmp_path / "ruyipage_per_tab_proxy_fp.txt").read_text(encoding="utf-8")
    assert "webdriver:0" in content
    assert "canvas:123" in content
    assert "proxy.rotate.enabled=false" not in content
    assert "old.example.com" not in content
    assert "proxy.rotate.enabled=true" in content
    assert "proxy.rotate.proxy=socks5://proxy.example.com:1000:user-a:pass-a" in content


def test_write_prefs_does_not_write_global_proxy_for_per_tab_rotate(tmp_path):
    opts = FirefoxOptions()
    opts.set_user_dir(str(tmp_path))
    opts.set_per_tab_proxies(["proxy.example.com:1000:user-a:pass-a"])
    opts.prepare_runtime_files()

    opts.write_prefs_to_profile()

    content = (tmp_path / "user.js").read_text(encoding="utf-8")
    assert "network.proxy.socks" not in content
    assert "network.proxy.socks_port" not in content
    assert "user-a" not in content
    assert "pass-a" not in content


def test_set_per_tab_proxies_validates_format():
    opts = FirefoxOptions()

    try:
        opts.set_per_tab_proxies(["bad-format"])
    except ValueError as e:
        assert "host:port:username:password" in str(e)
    else:
        raise AssertionError("expected ValueError for invalid proxy format")
