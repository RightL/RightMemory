/** The only title markup the editor interprets; arbitrary HTML stays literal. */
export type TopicMark = 'bold' | 'underline' | 'strike';
export interface WholeTitleFormat { inner: string; marks: Set<TopicMark> }

const wrappers: Record<TopicMark, [string, string]> = {
  bold: ['**', '**'], underline: ['<u>', '</u>'], strike: ['~~', '~~'],
};
const tags: Record<TopicMark, string> = { bold: 'strong', underline: 'u', strike: 's' };
const order: TopicMark[] = ['bold', 'underline', 'strike'];
type Part = string | { mark: TopicMark; start: number; end: number; children: Part[] };

function escapeText(text: string): string {
  return text.replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]!);
}

/** Whitespace distinguishes a nested same-mark opener from its closing delimiter.
 * Wrapping `A ~~old~~ branch` in strike therefore preserves the inner mark.
 */
function parse(raw: string): Part[] {
  const root: Part[] = [];
  const stack: { mark: TopicMark; start: number; opening: string; children: Part[] }[] = [];
  const parts = () => stack.at(-1)?.children ?? root;
  let offset = 0;
  for (const match of raw.matchAll(/<[^>]*>|\*{2,}|~{2,}/g)) {
    const run = match[0];
    const token = run.startsWith('**') ? '**' : run.startsWith('~~') ? '~~' : run;
    const paired = token === '**' || token === '~~';
    const count = paired ? Math.floor(run.length / 2) : 1;
    if (match.index > offset) parts().push(raw.slice(offset, match.index));
    const mark: TopicMark | undefined = token === '**' ? 'bold' : token === '~~' ? 'strike'
      : token === '<u>' || token === '</u>' ? 'underline' : undefined;
    const closingSide = /\S/.test(raw[match.index - 1] ?? '') || !/\S/.test(raw[match.index + run.length] ?? '');
    for (let index = 0; index < count; index++) {
      const start = match.index + index * token.length;
      const top = stack.at(-1);
      const blank = top && start > top.start + top.opening.length && !raw.slice(top.start + top.opening.length, start).trim();
      const closes = mark && top?.mark === mark && token !== '<u>' && (token === '</u>' || closingSide || blank);
      if (closes) {
        const frame = stack.pop()!;
        parts().push({ mark: frame.mark, start: frame.start, end: start + token.length, children: frame.children });
      } else if (mark && token !== '</u>') {
        stack.push({ mark, start, opening: token, children: [] });
      } else parts().push(token);
    }
    if (paired && run.length % 2) parts().push(run.at(-1)!);
    offset = match.index + run.length;
  }
  if (offset < raw.length) parts().push(raw.slice(offset));
  while (stack.length) {
    const frame = stack.pop()!;
    parts().push(frame.opening);
    for (const child of frame.children) parts().push(child);
  }
  return root;
}

function render(raw: string, markup: boolean): string {
  const work: (Part | { closing: string })[] = parse(raw).reverse();
  const output: string[] = [];
  // Iterative traversal also handles deeply nested manually authored titles.
  while (work.length) {
    const part = work.pop()!;
    if (typeof part === 'string') output.push(markup ? escapeText(part) : part);
    else if ('closing' in part) output.push(part.closing);
    else {
      if (markup) {
        output.push(`<${tags[part.mark]}>`);
        work.push({ closing: `</${tags[part.mark]}>` });
      }
      for (let index = part.children.length - 1; index >= 0; index--) work.push(part.children[index]);
    }
  }
  return output.join('');
}

export function titleMarkup(raw: string): string { return render(raw, true); }
export function titleText(raw: string): string { return render(raw, false); }

export function parseWholeTitleFormat(raw: string): WholeTitleFormat {
  let parts = parse(raw);
  let start = 0;
  let end = raw.length;
  const marks = new Set<TopicMark>();
  while (parts.length === 1 && typeof parts[0] !== 'string') {
    const part = parts[0];
    if (part.start !== start || part.end !== end) break;
    marks.add(part.mark);
    start += wrappers[part.mark][0].length;
    end -= wrappers[part.mark][1].length;
    parts = part.children;
  }
  return { inner: raw.slice(start, end), marks };
}

export function serializeWholeTitleFormat(value: WholeTitleFormat): string {
  return order.reduceRight((text, mark) => value.marks.has(mark) ? wrappers[mark][0] + text + wrappers[mark][1] : text, value.inner);
}

export function setWholeTitleMark(raw: string, mark: TopicMark, enabled: boolean): string {
  const value = parseWholeTitleFormat(raw);
  if (enabled) value.marks.add(mark);
  else value.marks.delete(mark);
  return serializeWholeTitleFormat(value);
}

export function toggleWholeTitleMark(raw: string, mark: TopicMark): string {
  return setWholeTitleMark(raw, mark, !parseWholeTitleFormat(raw).marks.has(mark));
}
