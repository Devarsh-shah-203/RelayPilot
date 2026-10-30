"""Semantic Markdown extraction from the provider's visible assistant DOM.

Plain DOM text drops list markers and table cell boundaries. This conservative
serializer preserves Markdown structure before sending it to Rich. The browser
still owns the DOM; Python receives only a string and never renders HTML.
"""

ANSWER_TO_MARKDOWN_JS = r"""root => {
    const clone = root.cloneNode(true);
    clone.querySelectorAll(
        'button, [role="button"], svg, nav, footer, [aria-hidden="true"], [data-copy-button], ' +
        '[data-testid*="reasoning" i], [data-testid*="thinking" i], ' +
        '[aria-label*="reasoning" i], [aria-label*="thinking" i], ' +
        '[class*="reasoning" i], [class*="thinking" i]'
    ).forEach(node => node.remove());

    const text = node => (node.textContent || '').replace(/\u00a0/g, ' ');
    // A plain-text DOM node must remain byte-for-byte compatible with the
    // previous gateway extractor. In particular, intentional spaces before
    // interior newlines may be part of an echoed prompt or response.
    if (clone.childNodes.length === 1 && clone.firstChild.nodeType === 3)
        return text(clone).replace(/\r\n?/g, '\n').trim();
    const inlineTags = new Set(['a', 'span', 'b', 'strong', 'em', 'i', 'u', 's', 'del', 'code', 'small', 'sup', 'sub']);
    const lines = value => String(value || '').replace(/\r\n?/g, '\n').replace(/[ \t]+\n/g, '\n').trim();
    function inline(node) {
        if (!node) return '';
        if (node.nodeType === 3) return node.nodeValue.replace(/\u00a0/g, ' ').replace(/[ \t\n]+/g, ' ');
        if (node.nodeType !== 1) return '';
        const tag = node.tagName.toLowerCase();
        if (tag === 'br') return '\n';
        const body = Array.from(node.childNodes).map(inline).join('');
        if (tag === 'strong' || tag === 'b') return body.trim() ? `**${body.trim()}**` : '';
        if (tag === 'em' || tag === 'i') return body.trim() ? `*${body.trim()}*` : '';
        if (tag === 's' || tag === 'del') return body.trim() ? `~~${body.trim()}~~` : '';
        if (tag === 'code' && node.parentElement?.tagName.toLowerCase() !== 'pre') {
            const run = '`'.repeat(Math.max(1, ...Array.from(text(node).matchAll(/`+/g), m => m[0].length + 1)));
            return run + text(node) + run;
        }
        if (tag === 'a') {
            const href = node.getAttribute('href') || '';
            // Avoid embedding arbitrary javascript/data URLs in terminal Markdown.
            if (/^https?:\/\//i.test(href) && body.trim()) return `[${body.trim()}](${href})`;
        }
        return body;
    }
    function table(el) {
        const rows = Array.from(el.querySelectorAll('tr')).map(row =>
            Array.from(row.children).filter(cell => /^(TD|TH)$/.test(cell.tagName)).map(cell =>
                lines(inline(cell)).replace(/\|/g, '\\|').replace(/\n/g, ' ')
            )
        ).filter(row => row.length);
        if (!rows.length) return '';
        const width = Math.max(...rows.map(row => row.length));
        const padded = row => Array.from({length: width}, (_, i) => row[i] || '');
        const fmt = row => '| ' + padded(row).join(' | ') + ' |';
        const hasHead = !!el.querySelector('thead') || !!el.querySelector('tr th');
        const header = hasHead ? rows[0] : Array.from({length: width}, (_, i) => `Column ${i + 1}`);
        const data = hasHead ? rows.slice(1) : rows;
        return [fmt(header), fmt(Array(width).fill('---')), ...data.map(fmt)].join('\n');
    }
    function block(el, level = 0) {
        if (!el) return '';
        if (el.nodeType === 3) return lines(el.nodeValue || '');
        if (el.nodeType !== 1) return '';
        const tag = el.tagName.toLowerCase();
        if (tag === 'pre') {
            const code = el.querySelector('code');
            const source = text(code || el).replace(/\r\n?/g, '\n').replace(/\n$/, '');
            const className = code?.getAttribute('class') || '';
            const lang = code?.getAttribute('data-language') || el.getAttribute('data-language') ||
                (className.match(/(?:language|lang)-([\w+#.-]+)/) || [])[1] || '';
            const maxTicks = Math.max(2, ...Array.from(source.matchAll(/`+/g), m => m[0].length));
            const fence = '`'.repeat(maxTicks + 1);
            return `${fence}${lang}\n${source}\n${fence}`;
        }
        if (tag === 'table') return table(el);
        if (/^h[1-6]$/.test(tag)) return '#'.repeat(Number(tag[1])) + ' ' + lines(inline(el));
        if (tag === 'hr') return '---';
        if (tag === 'ul' || tag === 'ol') {
            const start = Number(el.getAttribute('start') || 1);
            return Array.from(el.children).filter(item => item.tagName.toLowerCase() === 'li')
                .map((item, i) => {
                    const prefix = tag === 'ol' ? `${start + i}. ` : '- ';
                    const ind = '  '.repeat(level);
                    const chunks = [];
                    const nested = [];
                    for (const child of item.childNodes) {
                        if (child.nodeType === 1 && /^(UL|OL)$/.test(child.tagName)) nested.push(block(child, level + 1));
                        else if (child.nodeType === 1 && ['p', 'div'].includes(child.tagName.toLowerCase())) chunks.push(lines(inline(child)));
                        else chunks.push(inline(child));
                    }
                    const body = lines(chunks.join(' '));
                    return `${ind}${prefix}${body}` + (nested.length ? '\n' + nested.join('\n') : '');
                }).join('\n');
        }
        if (tag === 'blockquote') {
            const body = lines(children(el));
            return body.split('\n').map(line => '> ' + line).join('\n');
        }
        if (tag === 'p') return lines(inline(el));
        if (tag === 'br') return '\n';
        if (inlineTags.has(tag)) return inline(el);
        return children(el);
    }
    function children(el) {
        const parts = [];
        let inlinePart = '';
        const flush = () => { if (inlinePart.trim()) parts.push(lines(inlinePart)); inlinePart = ''; };
        for (const child of el.childNodes) {
            if (child.nodeType === 3) { inlinePart += child.nodeValue || ''; continue; }
            if (child.nodeType !== 1) continue;
            const tag = child.tagName.toLowerCase();
            if (inlineTags.has(tag)) { inlinePart += inline(child); continue; }
            if (tag === 'br') { inlinePart += '\n'; continue; }
            flush();
            const result = block(child);
            if (result.trim()) parts.push(result.trim());
        }
        flush();
        return parts.join('\n\n');
    }
    return lines(block(clone));
}"""
