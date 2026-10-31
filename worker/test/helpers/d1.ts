/**
 * A stand-in for Cloudflare D1 in tests. D1 is SQLite, so Node's built-in SQLite runs the very
 * same schema.sql and the very same queries as production.
 */
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";

class Statement {
  constructor(
    private readonly raw: DatabaseSync,
    private readonly sql: string,
    private readonly params: unknown[] = [],
  ) {}

  bind(...params: unknown[]): Statement {
    return new Statement(this.raw, this.sql, params);
  }

  async first<T>(): Promise<T | null> {
    const row = this.raw.prepare(this.sql).get(...(this.params as never[]));
    return (row ?? null) as T | null;
  }

  async all<T>(): Promise<{ results: T[]; success: boolean; meta: Record<string, unknown> }> {
    const rows = this.raw.prepare(this.sql).all(...(this.params as never[]));
    return { results: rows as T[], success: true, meta: {} };
  }

  async run(): Promise<{ success: boolean; meta: { changes: number; last_row_id: number } }> {
    const result = this.raw.prepare(this.sql).run(...(this.params as never[]));
    return { success: true, meta: { changes: Number(result.changes), last_row_id: Number(result.lastInsertRowid) } };
  }
}

export const SCHEMA_PATH = new URL("../../../schema.sql", import.meta.url);

export function createDb(): { db: D1Database; raw: DatabaseSync } {
  const raw = new DatabaseSync(":memory:");
  raw.exec(readFileSync(SCHEMA_PATH, "utf8"));
  const db = {
    prepare: (sql: string) => new Statement(raw, sql),
    exec: async (sql: string) => {
      raw.exec(sql);
      return { count: 1, duration: 0 };
    },
  };
  return { db: db as unknown as D1Database, raw };
}
