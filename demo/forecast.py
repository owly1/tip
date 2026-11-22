"""forecast-cli: prints today's weather. (Demo project for tip; the libraries are fictional.)"""
import sys

try:
    import weatherapi  # noqa: F401
    import tinycache  # noqa: F401
except ImportError:
    weatherapi = tinycache = None


def main() -> None:
    city = sys.argv[1] if len(sys.argv) > 1 else "Waterloo"
    print(f"{city}: 3°C, light snow")


if __name__ == "__main__":
    main()
