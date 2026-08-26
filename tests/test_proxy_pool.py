import unittest

from proxy_pool import ProxyPool, mask_proxy, parse_proxy_pool, to_playwright_proxy


class ProxyPoolTests(unittest.TestCase):
    def test_random_rotation_uses_each_proxy_before_reusing_one(self) -> None:
        proxies = ["http://proxy-a:8000", "http://proxy-b:8000", "http://proxy-c:8000"]
        pool = ProxyPool(proxies, True)

        selected = [pool.acquire() for _ in range(3)]

        self.assertEqual(set(selected), set(proxies))

    def test_blocked_proxy_is_skipped_until_batch_release(self) -> None:
        proxies = ["http://proxy-a:8000", "http://proxy-b:8000"]
        pool = ProxyPool(proxies, True)
        pool.block(proxies[0])

        self.assertEqual(pool.acquire(), proxies[1])
        self.assertTrue(pool.is_blocked(proxies[0]))

        pool.release_blocked()
        self.assertFalse(pool.is_blocked(proxies[0]))

    def test_normalizes_common_proxy_formats(self) -> None:
        proxies = parse_proxy_pool(
            "eu.proxy001.com:7878:mbqchl877859_custom_zone_GB_st_Scotland_sid_58952764_time_90:pwd512874\n"
            "user:password@proxy.example:8888\n"
            "socks5://socks.example:1080\n"
        )

        self.assertEqual(
            proxies,
            [
                "http://mbqchl877859_custom_zone_GB_st_Scotland_sid_58952764_time_90:pwd512874@eu.proxy001.com:7878",
                "http://user:password@proxy.example:8888",
                "socks5://socks.example:1080",
            ],
        )

    def test_builds_playwright_proxy_without_credentials_in_display(self) -> None:
        proxy = "eu.proxy001.com:7878:user:password"

        self.assertEqual(
            to_playwright_proxy(proxy),
            {
                "server": "http://eu.proxy001.com:7878",
                "username": "user",
                "password": "password",
            },
        )
        self.assertEqual(mask_proxy(proxy), "http://eu.proxy001.com:7878 (auth)")
