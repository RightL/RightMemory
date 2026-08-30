import DOMPurify from "dompurify";
import katex from "katex";
import { Marked, type Tokens } from "marked";

const MAX_MATH_LENGTH = 20_000;
export const RICH_TEXT_CACHE_LIMIT = 256;
const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:", "tel:"]);
const renderCache = new Map<string, DocumentFragment>();

interface MathToken extends Tokens.Generic {
  display: boolean;
  text: string;
}

interface MathMatch {
  raw: string;
  text: string;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    switch (character) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      default:
        return "&#39;";
    }
  });
}

function isEscaped(source: string, index: number): boolean {
  let slashCount = 0;
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor -= 1) {
    slashCount += 1;
  }
  return slashCount % 2 === 1;
}

function matchBlockMath(source: string, opener: string, closer: string): MathMatch | undefined {
  if (!source.startsWith(opener)) {
    return undefined;
  }

  const closeAt = source.indexOf(closer, opener.length);
  if (closeAt < 0 || isEscaped(source, closeAt)) {
    return undefined;
  }

  const text = source.slice(opener.length, closeAt).trim();
  if (!text) {
    return undefined;
  }

  let end = closeAt + closer.length;
  while (source[end] === " " || source[end] === "\t") {
    end += 1;
  }

  if (end < source.length && source[end] !== "\r" && source[end] !== "\n") {
    return undefined;
  }
  if (source[end] === "\r") {
    end += 1;
  }
  if (source[end] === "\n") {
    end += 1;
  }

  return { raw: source.slice(0, end), text };
}

function matchInlineDollarMath(source: string): MathMatch | undefined {
  if (!source.startsWith("$") || source.startsWith("$$") || /\s/u.test(source[1] ?? "")) {
    return undefined;
  }

  for (let cursor = 1; cursor < source.length; cursor += 1) {
    const character = source[cursor];
    if (character === "\r" || character === "\n") {
      return undefined;
    }
    if (character !== "$" || isEscaped(source, cursor)) {
      continue;
    }

    const preceding = source[cursor - 1] ?? "";
    const following = source[cursor + 1] ?? "";
    if (/\s/u.test(preceding) || character === following || /\d/u.test(following)) {
      return undefined;
    }

    return {
      raw: source.slice(0, cursor + 1),
      text: source.slice(1, cursor),
    };
  }

  return undefined;
}

function matchInlineParenthesizedMath(source: string): MathMatch | undefined {
  if (!source.startsWith("\\(")) {
    return undefined;
  }

  for (let cursor = 2; cursor < source.length - 1; cursor += 1) {
    if (source[cursor] === "\r" || source[cursor] === "\n") {
      return undefined;
    }
    if (!source.startsWith("\\)", cursor) || isEscaped(source, cursor)) {
      continue;
    }

    const text = source.slice(2, cursor).trim();
    if (!text) {
      return undefined;
    }
    return { raw: source.slice(0, cursor + 2), text };
  }

  return undefined;
}

function renderMath(token: MathToken): string {
  if (token.text.length > MAX_MATH_LENGTH) {
    return escapeHtml(token.raw);
  }

  try {
    const rendered = katex.renderToString(token.text, {
      displayMode: token.display,
      maxExpand: 200,
      maxSize: 20,
      strict: "ignore",
      throwOnError: false,
      trust: false,
    });
    return token.display ? `${rendered}\n` : rendered;
  } catch {
    return escapeHtml(token.raw);
  }
}

const richMarkdown = new Marked({
  async: false,
  breaks: true,
  gfm: true,
});

richMarkdown.use({
  extensions: [
    {
      name: "richDisplayMath",
      level: "block",
      tokenizer(source) {
        const match = matchBlockMath(source, "$$", "$$") ?? matchBlockMath(source, "\\[", "\\]");
        if (!match) {
          return undefined;
        }
        return {
          type: "richDisplayMath",
          raw: match.raw,
          text: match.text,
          display: true,
        };
      },
      renderer(token) {
        return renderMath(token as MathToken);
      },
    },
    {
      name: "richInlineMath",
      level: "inline",
      start(source) {
        const candidates = [source.indexOf("$"), source.indexOf("\\(")].filter((index) => index >= 0);
        return candidates.length ? Math.min(...candidates) : undefined;
      },
      tokenizer(source) {
        const match = matchInlineParenthesizedMath(source) ?? matchInlineDollarMath(source);
        if (!match) {
          return undefined;
        }
        return {
          type: "richInlineMath",
          raw: match.raw,
          text: match.text,
          display: false,
        };
      },
      renderer(token) {
        return renderMath(token as MathToken);
      },
    },
  ],
  renderer: {
    html({ text }) {
      return escapeHtml(text);
    },
  },
});

function safeBaseUrl(document: Document): URL {
  try {
    return new URL(document.baseURI);
  } catch {
    return new URL("http://localhost/");
  }
}

function secureLinks(fragment: DocumentFragment, document: Document): void {
  const baseUrl = safeBaseUrl(document);

  for (const link of fragment.querySelectorAll<HTMLAnchorElement>("a")) {
    const href = link.getAttribute("href")?.trim();
    link.removeAttribute("target");
    link.removeAttribute("rel");
    if (!href) {
      link.removeAttribute("href");
      continue;
    }

    let resolved: URL;
    try {
      resolved = new URL(href, baseUrl);
    } catch {
      link.removeAttribute("href");
      continue;
    }

    if (!SAFE_LINK_PROTOCOLS.has(resolved.protocol)) {
      link.removeAttribute("href");
      continue;
    }

    const isExternal =
      resolved.protocol === "mailto:" ||
      resolved.protocol === "tel:" ||
      !["http:", "https:"].includes(baseUrl.protocol) ||
      resolved.origin !== baseUrl.origin;
    if (isExternal) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  }
}

function cachedRender(document: Document, source: string): DocumentFragment | null {
  const key = `${document.baseURI}\u0000${source}`;
  const cached = renderCache.get(key);
  if (!cached) return null;
  renderCache.delete(key);
  renderCache.set(key, cached);
  return document.importNode(cached, true);
}

function rememberRender(document: Document, source: string, fragment: DocumentFragment): void {
  const key = `${document.baseURI}\u0000${source}`;
  renderCache.delete(key);
  renderCache.set(key, fragment.cloneNode(true) as DocumentFragment);
  while (renderCache.size > RICH_TEXT_CACHE_LIMIT) {
    const oldest = renderCache.keys().next().value;
    if (oldest === undefined) break;
    renderCache.delete(oldest);
  }
}

export function renderRichText(target: HTMLElement, source: string, cacheable = false): void {
  if (cacheable) {
    const cached = cachedRender(target.ownerDocument, source);
    if (cached) {
      target.replaceChildren(cached);
      return;
    }
  }

  let rendered: string;
  try {
    rendered = richMarkdown.parse(source, { async: false });
  } catch {
    target.textContent = source;
    return;
  }

  const fragment = DOMPurify.sanitize(rendered, {
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    FORBID_ATTR: ["src", "srcset"],
    FORBID_TAGS: ["animate", "animateMotion", "animateTransform", "audio", "button", "embed", "foreignObject", "form", "iframe", "image", "img", "input", "object", "picture", "script", "select", "set", "source", "style", "textarea", "video"],
    RETURN_DOM_FRAGMENT: true,
    SANITIZE_NAMED_PROPS: true,
    USE_PROFILES: { html: true, mathMl: true, svg: true, svgFilters: false },
  });
  secureLinks(fragment, target.ownerDocument);
  if (cacheable) rememberRender(target.ownerDocument, source, fragment);
  target.replaceChildren(fragment);
}
