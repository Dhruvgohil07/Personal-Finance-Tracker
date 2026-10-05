"""Build a SYNTHETIC ICICI `.xls` statement for tests (CLAUDE.md rule 8).

No real statement is ever committed. This module writes a workbook with the
same *layout* as the real ICICI download - the blank first column, the
header block, the label row at row 12, the legend block at the bottom, and
every cell stored as text - but with made-up names, amounts and account
numbers.

Why generate the file instead of committing a small `.xls`? A binary blob
in git cannot be reviewed in a diff, and when a test needs a slightly
different statement (a credit row, a broken date, a continuation row) the
only honest answer is a second blob. Here the fixture *is* readable Python:
a test passes the rows it wants and gets a real `.xls` in `tmp_path`.

`xlwt` is the writer (dev dependency only - the app itself never writes an
`.xls`). It is unmaintained, but it is pure Python, it still works on 3.12,
and it produces a genuine OLE2 workbook with the `D0 CF 11 E0` signature,
so `readers.read_xls` is exercised for real and not against a mock.
"""

from pathlib import Path
from typing import Final

import xlwt

# Exactly the labels the real file has in row 12, including the "(INR)"
# suffix - the parser matches on the start of a label, and the test should
# prove that against the real spelling.
HEADER_LABELS: Final = (
    "S No.",
    "Value Date",
    "Transaction Date",
    "Cheque Number",
    "Transaction Remarks",
    "Withdrawal Amount(INR)",
    "Deposit Amount(INR)",
    "Balance(INR)",
)

# One row = the eight cells of the table, written as text just like ICICI.
# A continuation row (a narration too long for one cell) is a row where only
# the remarks cell is filled - see the last two rows of DEFAULT_ROWS.
DEFAULT_ROWS: Final = (
    # serial, value date, txn date, cheque, remarks, withdrawal, deposit, balance
    ("1", "01/08/2026", "01/08/2026", "", "UPI/TESTSHOP/PAY", "70.00", "0.00", "111471.33"),
    ("2", "02/08/2026", "02/08/2026", "", "UPI/CHAISTALL/PAY", "20.00", "0.00", "111451.33"),
    ("3", "02/08/2026", "02/08/2026", "", "UPI/CHAISTALL/PAY", "20.00", "0.00", "111431.33"),
    ("4", "03/08/2026", "03/08/2026", "", "NEFT-SALARY-ACME", "0.00", "50000.00", "161431.33"),
    # A narration the bank split across two rows, cut mid-token: joined
    # directly it reads "...LTD/205412345678/PAYMENT", and a space inserted
    # at the join would turn one token into two.
    ("5", "04/08/2026", "04/08/2026", "", "MMT/IMPS/ACME BANK LTD", "1500.00", "0.00", "159931.33"),
    ("", "", "", "", "/205412345678/PAYMENT", "", "", ""),
)

LEGEND_LINES: Final = (
    "Legends Used in Account Statement",
    "1. INFT - Internal Fund Transfer (Within ICICI Bank)",
    "2. BPAY - Bill payment",
)


def build_icici_xls(
    path: Path,
    rows: tuple[tuple[str, ...], ...] = DEFAULT_ROWS,
    *,
    account_number: str = "084600001234",
    holder: str = "SYNTHETIC TEST USER",
    period_from: str = "01/08/2026",
    period_to: str = "31/08/2026",
    header_labels: tuple[str, ...] = HEADER_LABELS,
    with_legend: bool = True,
    with_period: bool = True,
    start_column: int = 1,
) -> Path:
    """Write a synthetic ICICI statement to `path` and return that path.

    Every argument has a default that mirrors the real file, so a test only
    passes what it is actually testing:

        build_icici_xls(tmp_path / "s.xls")                     # a normal file
        build_icici_xls(tmp_path / "s.xls", rows=BROKEN_ROWS)   # a bad row
        build_icici_xls(tmp_path / "s.xls", with_legend=False)  # no footer
        build_icici_xls(tmp_path / "s.xls", with_period=False)  # no period row
        build_icici_xls(tmp_path / "s.xls", start_column=3)     # table shifted right

    `start_column` is where the table is written; 1 is the real file's layout,
    because column 0 is empty in ICICI's download. It is an argument rather than
    a constant so that a test can shift the whole table sideways: that is the
    only way to prove the parser really finds its columns by header label, since
    a parser using hard-coded indexes 1-8 passes every fixture built at the
    default.
    """
    book = xlwt.Workbook()
    sheet = book.add_sheet("OpTransactionHistory")

    label_column = start_column
    sheet.write(1, label_column, "DETAILED STATEMENT")
    sheet.write(3, label_column, "Account Number")
    # The real cell holds the number, the currency and the holder's name
    # together - which is why the parser must pull out four digits and drop
    # the rest (SPEC §7.3).
    sheet.write(3, label_column + 2, f"{account_number} ( INR )  - {holder}")
    if with_period:
        sheet.write(4, label_column, "Transaction Date from")
        sheet.write(4, label_column + 2, period_from)
        sheet.write(4, label_column + 3, "to")
        sheet.write(4, label_column + 4, period_to)
    sheet.write(11, label_column, f"Transactions List - {holder} - {account_number}")

    for column, label in enumerate(header_labels, start=start_column):
        sheet.write(12, column, label)

    row_index = 13
    for row in rows:
        for column, value in enumerate(row, start=start_column):
            if value != "":
                sheet.write(row_index, column, value)
        row_index += 1

    if with_legend:
        for line in LEGEND_LINES:
            sheet.write(row_index, label_column, line)
            row_index += 1

    book.save(str(path))
    return path
