import sqlite3


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)

    def account(self, name):
        cur = self.conn.execute("SELECT id, balance FROM accounts WHERE name = '%s'" % name)
        return cur.fetchone()

    def entries(self, account_id, limit=50):
        cur = self.conn.execute("SELECT * FROM entries WHERE account = ? LIMIT ?", (account_id, limit))
        return cur.fetchall()

    def post(self, account_id, amount, memo):
        self.conn.execute("INSERT INTO entries (account, amount, memo) VALUES (?, ?, ?)", (account_id, amount, memo))

    def close(self):
        pass
