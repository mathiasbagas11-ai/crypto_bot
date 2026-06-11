"""Test _suggest_command — saran command saat user salah ketik.

Konteks: command tak dikenal (mis. /logshoot) dulu dilempar ke AI chat
sehingga DeepSeek bingung. Sekarang router kasih saran command terdekat.
"""
import crypto_screening_bot_v13 as bot


def test_typo_logshoot_suggests_logshot():
    assert bot._suggest_command("/logshoot") == "/logshot"


def test_various_typos_resolve():
    assert bot._suggest_command("/logshat") == "/logshot"
    assert bot._suggest_command("/trads") == "/trades"
    assert bot._suggest_command("/logtradee") == "/logtrade"


def test_unrelated_gibberish_returns_none():
    assert bot._suggest_command("/xyzzy") is None
    assert bot._suggest_command("/wat") is None


def test_case_insensitive():
    assert bot._suggest_command("/LOGSHOOT") == "/logshot"
