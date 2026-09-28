"""Tests for src/utils.py — HTML escaping, cover validation, translation."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest

from src.utils import (
    get_http_session, html_escape, is_placeholder_image,
    is_unreliable_gb_cover, is_english_description,
)


class TestReusableHttpSession(unittest.TestCase):
    def test_session_is_reused_within_the_same_worker_thread(self):
        self.assertIs(get_http_session(), get_http_session())


class TestHtmlEscape(unittest.TestCase):
    def test_escapes_ampersand(self):
        self.assertEqual(html_escape("A & B"), "A &amp; B")

    def test_escapes_less_than(self):
        self.assertEqual(html_escape("<tag>"), "&lt;tag&gt;")

    def test_escapes_greater_than(self):
        self.assertEqual(html_escape("a > b"), "a &gt; b")

    def test_escapes_double_quote(self):
        self.assertEqual(html_escape('say "hello"'), "say &quot;hello&quot;")

    def test_escapes_multiple_chars(self):
        self.assertEqual(
            html_escape('<script>alert("XSS")</script>'),
            "&lt;script&gt;alert(&quot;XSS&quot;)&lt;/script&gt;"
        )

    def test_passthrough_plain_text(self):
        self.assertEqual(html_escape("Hello World"), "Hello World")

    def test_empty_string(self):
        self.assertEqual(html_escape(""), "")

    def test_already_escaped(self):
        self.assertEqual(html_escape("&lt;tag&gt;"), "&amp;lt;tag&amp;gt;")

    def test_unicode(self):
        self.assertEqual(html_escape("Caf\xe9"), "Caf\xe9")  # unchanged — no special chars

    def test_long_string(self):
        long_text = "a" * 10_000
        result = html_escape(long_text)
        self.assertEqual(result, long_text)


class TestPlaceholderImage(unittest.TestCase):
    def test_empty_bytes_returns_true(self):
        self.assertTrue(is_placeholder_image(b""))

    def test_small_bytes_returns_true(self):
        self.assertTrue(is_placeholder_image(b"xxx"))

    def test_none_returns_true(self):
        self.assertTrue(is_placeholder_image(None))

    def test_real_jpeg_not_flagged(self):
        # JPEG magic bytes + larger than 100 bytes — placeholder check is about size < 100
        # so a real JPEG large enough should not be flagged
        self.assertFalse(is_placeholder_image(b"\xff\xd8\xff\xe0" + b"x" * 120))

    def test_random_bytes_short_are_placeholder(self):
        # is_placeholder_image returns True for content < 100 bytes by design
        self.assertTrue(is_placeholder_image(b"short"))

    def test_large_random_bytes_not_placeholder(self):
        # Large random bytes that don't match any placeholder fingerprint
        self.assertFalse(is_placeholder_image(b"random content here " * 20))


class TestUnreliableCover(unittest.TestCase):
    def test_endswith_aacaj(self):
        self.assertTrue(is_unreliable_gb_cover("abcAACAAJ"))

    def test_does_not_end_aacaj(self):
        self.assertFalse(is_unreliable_gb_cover("abc123XYZ"))

    def test_empty_string(self):
        self.assertFalse(is_unreliable_gb_cover(""))


class TestEnglishDetection(unittest.TestCase):
    """is_english_description skips translation for English text."""

    def test_plain_english(self):
        text = "This is a sample book description about a murder mystery."
        self.assertTrue(is_english_description(text))

    def test_english_with_punctuation(self):
        text = "The quick brown fox — a story of survival and redemption. It's great!"
        self.assertTrue(is_english_description(text))

    def test_empty_returns_true(self):
        self.assertTrue(is_english_description(""))
        self.assertTrue(is_english_description(None))

    def test_chinese_returns_false(self):
        text = "這是一本關於愛與戰爭的書"
        self.assertFalse(is_english_description(text))

    def test_japanese_returns_false(self):
        text = "彼女は有名な小説家です"
        self.assertFalse(is_english_description(text))

    def test_korean_returns_false(self):
        text = "이 책은 사랑과 운명에 관한 것이다"
        self.assertFalse(is_english_description(text))

    def test_arabic_returns_false(self):
        text = "هذا كتاب عن الحب والحرب"
        self.assertFalse(is_english_description(text))

    def test_hebrew_returns_false(self):
        text = "ספר על אהבה ומלחמה"
        self.assertFalse(is_english_description(text))

    def test_cyrillic_returns_false(self):
        text = "Это книга о любви и войне"
        self.assertFalse(is_english_description(text))

    def test_thai_returns_false(self):
        text = "หนังสือเล่มนี้เกี่ยวกับความรัก"
        self.assertFalse(is_english_description(text))

    def test_short_ascii_is_english(self):
        text = "A tale of loss."
        self.assertTrue(is_english_description(text))

    def test_german_is_not_english(self):
        text = "Das ist ein Buch über die Liebe und den Krieg in Deutschland."
        self.assertFalse(is_english_description(text))

    def test_french_is_not_english(self):
        text = "C'est un livre sur l'amour et la guerre en France."
        self.assertFalse(is_english_description(text))

    def test_spanish_is_not_english(self):
        text = "Es un libro sobre el amor y la guerra en España."
        self.assertFalse(is_english_description(text))


if __name__ == "__main__":
    unittest.main()
