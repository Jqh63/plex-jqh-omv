# X-Client-Label — the optional device name that turns an opaque cid into
# "who woke the server". It is client-controlled and lands verbatim in the
# journal, so the sanitiser is the whole security story here: these tests
# pin the closed charset rather than the happy path.
import logging

from app import clean_label


def test_plain_name_passes_through():
    assert clean_label("iPhone de Marie") == "iPhone de Marie"


def test_french_accents_survive():
    # The family names devices in French; stripping accents would mangle
    # them into something nobody recognises in the log.
    assert clean_label("Téléphone Mémé") == "Téléphone Mémé"


def test_every_allowed_char_is_header_safe():
    # Each surviving char must be <U+0100, else fetch() throws client-side
    # when the value is put in an HTTP header byte string.
    out = clean_label("Téléphone-de_Mémé 12")
    assert out and all(ord(c) < 0x100 for c in out)


def test_newline_cannot_forge_a_log_line():
    # Log injection: a label carrying CR/LF would let a caller append a
    # fabricated journal entry after the real one.
    assert "\n" not in clean_label("ok\nwol ip=1.2.3.4 status=200")
    assert "\r" not in clean_label("ok\rfake")


def test_quote_cannot_break_out_of_the_quoted_field():
    # The log formats the label as label="...". A raw quote would end the
    # field early and let the rest read as separate key=value pairs.
    assert '"' not in clean_label('x" status=200 cid="forged')


def test_length_is_capped():
    assert len(clean_label("A" * 200)) == 24


def test_missing_or_empty_becomes_placeholder():
    assert clean_label(None) == "-"
    assert clean_label("") == "-"
    assert clean_label("!!!") == "-"  # nothing survives the charset


def test_wol_log_line_carries_the_label(caplog, monkeypatch):
    from fastapi.testclient import TestClient

    import app as relay

    # No DNS and no UDP from the test suite — same stubs the campaign
    # tests use, so /wol reaches its success path.
    monkeypatch.setattr(relay.socket, "gethostbyname", lambda h: "192.0.2.1")
    monkeypatch.setattr(relay, "_send_packets", lambda ip, pkt: None)

    with caplog.at_level(logging.INFO, logger=relay.logger.name):
        with TestClient(relay.app) as client:
            resp = client.post(
                "/wol",
                json={"mac": "aa:bb:cc:dd:ee:ff"},
                headers={
                    "X-Token": "test-token",
                    "X-Client-Id": "cid-test",
                    "X-Client-Label": "iPhone de Marie",
                },
            )
    # The log line is only emitted on the success path — assert the request
    # actually got there, else the assertion below could pass vacuously.
    assert resp.status_code == 200
    lines = [r.getMessage() for r in caplog.records]
    wol = [m for m in lines if m.startswith("wol ")]
    assert wol, f"no wol log line emitted; got {lines}"
    assert 'label="iPhone de Marie"' in wol[0]
