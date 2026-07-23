"""Compatibility entry point for running pi chat directly."""

from pi_chat.app import app


def main():
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=9000, reload=False)


if __name__ == "__main__":
    main()
