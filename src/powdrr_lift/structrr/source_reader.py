"""Local command entry point for the service-owned source reader."""

from powdrr_service.structrr.source_reader import main as reader_main


def main(argv: list[str] | None = None) -> int:
    def check() -> int:
        from powdrr_lift.workrr.source_reference_validation import main_check

        return main_check([])

    return reader_main(argv, reference_check=check)


if __name__ == "__main__":
    raise SystemExit(main())
