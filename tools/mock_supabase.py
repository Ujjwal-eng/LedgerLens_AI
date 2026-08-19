"""
A fake Supabase client that mimics supabase-py's chainable query builder
(.table().insert().execute(), .table().select().eq().execute()) closely
enough that fetch_vendor_history() and save_processed_invoice() run
completely unmodified against it.
"""


class _MockResponse:
    def __init__(self, data):
        self.data = data


class _MockTable:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filter = None

    def insert(self, row: dict):
        self._rows.append(row)
        return self

    def select(self, *_args):
        return self

    def eq(self, field: str, value):
        self._filter = (field, value)
        return self

    def execute(self):
        if self._filter:
            field, value = self._filter
            data = [r for r in self._rows if r.get(field) == value]
        else:
            data = list(self._rows)
        return _MockResponse(data)


class MockSupabaseClient:
    def __init__(self):
        self._rows: list[dict] = []

    def table(self, _name: str):
        return _MockTable(self._rows)
