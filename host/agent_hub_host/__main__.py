"""Public module entry point with a small latency-sensitive summary path."""
import sys


def main():
    if sys.argv[1:3] == ['intake', 'summary']:
        from .summary_fast import try_fast_summary
        if try_fast_summary():
            return
        from .intake import summary_main
        summary_main()
    else:
        from .cli import main as cli_main
        cli_main()


if __name__ == '__main__':
    main()
