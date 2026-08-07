from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "jforex" / "LucidBridgeStrategy.java"


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing required JForex bridge text: {needle}")


def main() -> None:
    text = SRC.read_text(encoding="utf-8")

    required = [
        "public class LucidBridgeStrategy implements IStrategy",
        "Instrument.USA500IDXUSD",
        "Instrument.USATECHIDXUSD",
        "Instrument.LIGHTCMDUSD",
        'marketByInstrument.put(Instrument.USA500IDXUSD, "es")',
        'marketByInstrument.put(Instrument.USATECHIDXUSD, "nq")',
        'marketByInstrument.put(Instrument.LIGHTCMDUSD, "cl")',
        "context.setSubscribedInstruments(instruments, true)",
        "public void onTick(Instrument instrument, ITick tick)",
        "tick.getBid()",
        "tick.getAsk()",
        "tick.getBidVolume()",
        "tick.getAskVolume()",
        "tick.getTime()",
        'DEFAULT_ENDPOINT = "http://127.0.0.1:8765/tick"',
        '"X-Lucid-Bridge-Token"',
    ]
    for needle in required:
        assert_contains(text, needle)

    forbidden = [
        ".submitOrder(",
        "IEngine.OrderCommand",
        "context.getEngine()",
        "createOrder",
        "BUY",
        "SELL",
    ]
    hits = [needle for needle in forbidden if needle in text]
    if hits:
        raise AssertionError(f"JForex bridge must not trade; forbidden text found: {hits}")

    print("Lucid JForex bridge source audit passed.")


if __name__ == "__main__":
    main()
