"""Test routing gambar di process_update — Photo (compressed) & File/Document.

Bug: screenshot yang dikirim sebagai File/Document (uncompressed) tidak masuk
field message["photo"], jadi diabaikan total — tidak ada respons & tidak masuk
spreadsheet. Sekarang document ber-mime image/* ditangani sama seperti photo.
"""
import crypto_screening_bot_v13 as bot


class _FakeThread:
    """Jalankan target langsung (sinkron) supaya test deterministik."""
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _setup(monkeypatch, awaiting=False, in_wizard=False):
    calls = []
    monkeypatch.setattr(bot.threading, "Thread", _FakeThread)
    monkeypatch.setattr(bot, "is_allowed", lambda c: True)
    monkeypatch.setattr(bot, "JOURNAL_MODULE", True)
    monkeypatch.setattr(bot, "is_wizard_expecting_image", lambda c: in_wizard)
    monkeypatch.setattr(bot, "_awaiting_tradeshot", {"123": True} if awaiting else {})
    monkeypatch.setattr(bot, "handle_trade_screenshot",
                        lambda fid, cid: calls.append(("shot", fid, cid)))
    monkeypatch.setattr(bot, "handle_chart_command",
                        lambda cid, fid: calls.append(("chart", cid, fid)))
    monkeypatch.setattr(bot, "handle_journal_wizard_image",
                        lambda fid, cid: calls.append(("wizard", fid, cid)))
    return calls


def _msg(update_id, **message):
    message.setdefault("chat", {"id": 123})
    return {"update_id": update_id, "message": message}


def test_document_image_routed_to_screenshot(monkeypatch):
    calls = _setup(monkeypatch, awaiting=True)
    bot.process_update(_msg(1001, document={"file_id": "F1", "mime_type": "image/png"}))
    assert calls == [("shot", "F1", "123")]


def test_compressed_photo_still_routed_to_screenshot(monkeypatch):
    calls = _setup(monkeypatch, awaiting=True)
    bot.process_update(_msg(1002, photo=[{"file_id": "P_small"}, {"file_id": "P_big"}]))
    # ambil resolusi terbesar (terakhir)
    assert calls == [("shot", "P_big", "123")]


def test_image_without_awaiting_goes_to_chart(monkeypatch):
    calls = _setup(monkeypatch, awaiting=False)
    bot.process_update(_msg(1003, document={"file_id": "F2", "mime_type": "image/jpeg"}))
    assert calls == [("chart", "123", "F2")]


def test_non_image_document_ignored(monkeypatch):
    calls = _setup(monkeypatch, awaiting=True)
    # PDF bukan gambar → jangan diperlakukan sebagai screenshot
    bot.process_update(_msg(1004, document={"file_id": "D1", "mime_type": "application/pdf"}))
    assert calls == []


def test_wizard_image_takes_priority(monkeypatch):
    calls = _setup(monkeypatch, awaiting=True, in_wizard=True)
    bot.process_update(_msg(1005, document={"file_id": "F3", "mime_type": "image/webp"}))
    assert calls == [("wizard", "F3", "123")]
