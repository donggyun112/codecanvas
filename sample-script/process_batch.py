"""Sample non-API entrypoint for CodeCanvas."""
from helpers import load_items, write_report


class BatchReport:
    """Container for the generated batch summary."""

    def __init__(self, items):
        self.items = items


def summarize_items(items):
    """Summarize the loaded items into a compact report."""
    return {"count": len(items), "items": items}


def main():
    """Run the batch report generation script."""
    def normalize_item(item):
        """Normalize one item before it is added to the report."""
        return item.strip().upper()

    items = load_items()
    normalized_items = []
    for item in items:
        normalized_items.append(normalize_item(item))

    report = BatchReport(normalized_items)
    summary = summarize_items(report.items)
    return write_report(summary)


if __name__ == "__main__":
    main()
