from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    print("IoTCA entrypoints:")
    print(f"- Cloud server: uv run python3 -m uvicorn server:app --host 0.0.0.0 --port 5000")
    print(f"- Pi mini-server: uv run python3 -m uvicorn pi_mini_server:app --host 0.0.0.0 --port 6000")
    print(f"- Pi exporter: uv run python3 scripts/pi_exporter.py")
    print(f"Project root: {root}")


if __name__ == "__main__":
    main()
