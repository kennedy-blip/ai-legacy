import { Fragment, type ReactNode } from "react";

// A small Markdown renderer for answers. It builds React elements only (no HTML
// injection), so untrusted model output can never run script. Supported: headings,
// paragraphs, lists, block quotes, fenced code, tables, rules, bold, italic, inline
// code, http(s) links and bracketed references such as [File.java:12].

type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "ref"; text: string; href: string | null }
  | { kind: "strong" | "em"; children: Inline[] };

interface Delimiter {
  kind: "delim";
  marker: string;
  open: boolean;
  close: boolean;
}

type Token = Inline | Delimiter;

const WORD = /[\p{L}\p{N}]/u;
const ESCAPABLE = /[\\`*_[\]()#+\-.!>|]/;

function tokenize(source: string): Token[] {
  const tokens: Token[] = [];
  let text = "";
  const flush = () => {
    if (text) {
      tokens.push({ kind: "text", text });
      text = "";
    }
  };
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    if (ch === "\\" && i + 1 < source.length && ESCAPABLE.test(source[i + 1])) {
      text += source[i + 1];
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 1;
      while (source[i + run] === "`") run += 1;
      const fence = "`".repeat(run);
      const end = source.indexOf(fence, i + run);
      if (end >= 0) {
        flush();
        tokens.push({ kind: "code", text: source.slice(i + run, end).trim() });
        i = end + run;
      } else {
        text += fence;
        i += run;
      }
      continue;
    }
    if (ch === "[") {
      const end = source.indexOf("]", i + 1);
      const label = end > i + 1 ? source.slice(i + 1, end) : "";
      if (label && label.length <= 200 && !label.includes("[") && !label.includes("\n")) {
        let next = end + 1;
        const link = /^\((https?:\/\/[^\s()]+)\)/.exec(source.slice(next, next + 2048));
        if (link) next += link[0].length;
        flush();
        tokens.push({ kind: "ref", text: label, href: link ? link[1] : null });
        i = next;
        continue;
      }
    }
    if (ch === "*" || ch === "_") {
      const run = source[i + 1] === ch ? 2 : 1;
      const before = i === 0 ? " " : source[i - 1];
      const after = i + run >= source.length ? " " : source[i + run];
      // Underscores inside identifiers (snake_case) are never emphasis.
      const open = !/\s/.test(after) && (ch === "*" || !WORD.test(before));
      const close = !/\s/.test(before) && (ch === "*" || !WORD.test(after));
      if (open || close) {
        flush();
        tokens.push({ kind: "delim", marker: ch.repeat(run), open, close });
      } else {
        text += ch.repeat(run);
      }
      i += run;
      continue;
    }
    text += ch;
    i += 1;
  }
  flush();
  return tokens;
}

function literal(token: Token): Inline {
  return token.kind === "delim" ? { kind: "text", text: token.marker } : token;
}

/** Pair emphasis delimiters; anything left unpaired stays as the character it was. */
export function parseInline(source: string): Inline[] {
  const out: Token[] = [];
  const openers: number[] = [];
  for (const token of tokenize(source)) {
    if (token.kind !== "delim") {
      out.push(token);
      continue;
    }
    if (token.close) {
      let slot = openers.length - 1;
      while (slot >= 0 && (out[openers[slot]] as Delimiter).marker !== token.marker) slot -= 1;
      if (slot >= 0) {
        const at = openers[slot];
        const children = out.splice(at + 1).map(literal);
        out.pop();
        openers.length = slot;
        out.push({ kind: token.marker.length === 2 ? "strong" : "em", children });
        continue;
      }
    }
    if (token.open) {
      openers.push(out.length);
      out.push(token);
    } else {
      out.push({ kind: "text", text: token.marker });
    }
  }
  return out.map(literal);
}

interface ListItem {
  depth: number;
  marker: string | null;
  text: string;
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; items: ListItem[] }
  | { kind: "code"; language: string; text: string }
  | { kind: "quote"; text: string }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "rule" };

const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE = /^ {0,3}(```+|~~~+)\s*([\w+#.-]*)/;
const BULLET = /^(\s*)(?:[-*+]|(\d{1,9})[.)])\s+(.*)$/;
const RULE = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function cells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((cell) => cell.trim().replaceAll("\\|", "|"));
}

export function parseBlocks(source: string): Block[] {
  const lines = source.replaceAll("\r\n", "\n").split("\n");
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  const endParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      endParagraph();
      const body: string[] = [];
      i += 1;
      // An unterminated fence (an answer still streaming) runs to the end.
      while (i < lines.length && !lines[i].trimStart().startsWith(fence[1])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push({ kind: "code", language: fence[2], text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      endParagraph();
      i += 1;
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      endParagraph();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      endParagraph();
      blocks.push({ kind: "rule" });
      i += 1;
      continue;
    }
    if (line.trimStart().startsWith("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      endParagraph();
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trimStart().startsWith("|")) {
        rows.push(cells(lines[i]));
        i += 1;
      }
      blocks.push({ kind: "table", header, rows });
      continue;
    }
    if (/^ {0,3}>/.test(line)) {
      endParagraph();
      const body: string[] = [];
      while (i < lines.length && /^ {0,3}>/.test(lines[i])) {
        body.push(lines[i].replace(/^ {0,3}>\s?/, ""));
        i += 1;
      }
      blocks.push({ kind: "quote", text: body.join(" ") });
      continue;
    }
    if (BULLET.test(line)) {
      endParagraph();
      const items: ListItem[] = [];
      while (i < lines.length) {
        const bullet = BULLET.exec(lines[i]);
        if (bullet) {
          const indent = bullet[1].replaceAll("\t", "    ").length;
          items.push({ depth: Math.min(Math.floor(indent / 2), 4), marker: bullet[2] ?? null, text: bullet[3] });
        } else if (lines[i].trim() && /^\s+/.test(lines[i]) && items.length) {
          // An indented continuation line belongs to the item above.
          items[items.length - 1].text += ` ${lines[i].trim()}`;
        } else {
          break;
        }
        i += 1;
      }
      blocks.push({ kind: "list", items });
      continue;
    }
    paragraph.push(line.trim());
    i += 1;
  }
  endParagraph();
  return blocks;
}

export interface MarkdownProps {
  text: string;
  /**
   * Render a bracketed reference such as `[File.java:12]`. Return null to leave the
   * text as written (most square brackets in prose are not references).
   */
  reference?: (raw: string, key: string) => ReactNode | null;
}

function renderInline(nodes: Inline[], prefix: string, reference: MarkdownProps["reference"]): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${prefix}.${index}`;
    switch (node.kind) {
      case "text":
        return <Fragment key={key}>{node.text}</Fragment>;
      case "code":
        return (
          <code key={key} className="rounded bg-raised px-1 py-px font-mono text-[0.9em] text-fg">
            {node.text}
          </code>
        );
      case "strong":
        return (
          <strong key={key} className="font-semibold text-fg">
            {renderInline(node.children, key, reference)}
          </strong>
        );
      case "em":
        return <em key={key}>{renderInline(node.children, key, reference)}</em>;
      case "ref": {
        if (node.href) {
          return (
            <a
              key={key}
              href={node.href}
              target="_blank"
              rel="noreferrer noopener"
              className="text-accent underline underline-offset-2"
            >
              {node.text}
            </a>
          );
        }
        const rendered = reference?.(node.text, key);
        return rendered ?? <Fragment key={key}>[{node.text}]</Fragment>;
      }
    }
  });
}

const HEADING_CLASS: Record<number, string> = {
  1: "mt-5 text-lg font-semibold tracking-tight first:mt-0",
  2: "mt-5 text-base font-semibold tracking-tight first:mt-0",
  3: "mt-5 text-[15px] font-semibold first:mt-0",
};

export function Markdown({ text, reference }: MarkdownProps) {
  const inline = (source: string, key: string) => renderInline(parseInline(source), key, reference);
  return (
    <div className="flex flex-col gap-2.5 text-[14.5px] leading-relaxed text-dim">
      {parseBlocks(text).map((block, index) => {
        const key = `b${index}`;
        switch (block.kind) {
          case "heading": {
            const className = `${HEADING_CLASS[block.level] ?? "mt-3 text-sm font-semibold first:mt-0"} text-fg`;
            if (block.level <= 1) {
              return (
                <h3 key={key} className={className}>
                  {inline(block.text, key)}
                </h3>
              );
            }
            if (block.level === 2) {
              return (
                <h4 key={key} className={className}>
                  {inline(block.text, key)}
                </h4>
              );
            }
            return (
              <h5 key={key} className={className}>
                {inline(block.text, key)}
              </h5>
            );
          }
          case "paragraph":
            return <p key={key}>{inline(block.text, key)}</p>;
          case "quote":
            return (
              <blockquote key={key} className="border-l-2 border-line-strong pl-3 italic">
                {inline(block.text, key)}
              </blockquote>
            );
          case "rule":
            return <hr key={key} className="border-line" />;
          case "code":
            return (
              <pre
                key={key}
                className="overflow-x-auto rounded-lg border border-line bg-ink p-3 font-mono text-[12.5px] leading-relaxed text-fg"
              >
                <code>{block.text}</code>
              </pre>
            );
          case "table":
            return (
              <div key={key} className="overflow-x-auto">
                <table className="w-full border-collapse text-left text-[13.5px]">
                  <thead>
                    <tr className="border-b border-line-strong">
                      {block.header.map((cell, column) => (
                        <th key={`${key}.h${column}`} className="px-2 py-1.5 font-semibold text-fg">
                          {inline(cell, `${key}.h${column}`)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={`${key}.r${r}`} className="border-b border-line">
                        {row.map((cell, column) => (
                          <td key={`${key}.r${r}.${column}`} className="px-2 py-1.5 align-top">
                            {inline(cell, `${key}.r${r}.${column}`)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "list":
            return (
              <ul key={key} className="flex flex-col gap-1">
                {block.items.map((item, position) => (
                  <li
                    key={`${key}.${position}`}
                    className="flex gap-2"
                    style={item.depth ? { marginLeft: `${item.depth * 1.25}rem` } : undefined}
                  >
                    <span className="shrink-0 select-none text-faint" aria-hidden="true">
                      {item.marker ? `${item.marker}.` : "•"}
                    </span>
                    <span className="min-w-0">{inline(item.text, `${key}.${position}`)}</span>
                  </li>
                ))}
              </ul>
            );
        }
      })}
    </div>
  );
}
