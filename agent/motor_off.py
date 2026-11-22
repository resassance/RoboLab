import sys

ENABLE_PINS = (16, 13)


def main() -> int:
    import RPi.GPIO as gpio

    gpio.setmode(gpio.BCM)
    status = 0
    configured = []
    for pin in ENABLE_PINS:
        try:
            gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            configured.append(pin)
        except Exception as exc:
            print(f"BCM {pin}: {exc}", file=sys.stderr)
            status = 1
    for pin in configured:
        try:
            if gpio.input(pin) != gpio.LOW:
                raise RuntimeError("readback is not LOW")
        except Exception as exc:
            print(f"BCM {pin}: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
