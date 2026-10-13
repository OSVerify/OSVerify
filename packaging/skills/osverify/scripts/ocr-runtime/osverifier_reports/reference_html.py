"""Conservative scholarly HTML extraction; source text is never executable input."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        # Iterative pre-order: unclosed legacy tags can nest thousands deep.
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in reversed(node.children) if isinstance(child, Node))


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict((k, v or '') for k, v in attrs))
        self.stack[-1].children.append(node)
        if tag not in {'meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'col'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _post_order(node):
    """Nodes with every descendant before its ancestor, without recursion."""
    order, stack = [], [node]
    while stack:
        current = stack.pop()
        order.append(current)
        stack.extend(child for child in current.children if isinstance(child, Node))
    return reversed(order)


def _render(node, text):
    """One node's text from its already rendered children (``text`` looks them up)."""
    if node.tag == 'script':
        return '$' + ''.join(str(c) for c in node.children) + '$' if 'math/tex' in node.attrs.get('type', '') else ''
    if node.tag in {'style', 'noscript', 'nav', 'footer', 'form', 'button', 'svg'} or node.attrs.get('aria-hidden') == 'true':
        return ''
    if node.tag == 'math':
        latex = node.attrs.get('alttext')
        if not latex:
            latex = next((text(n) for n in node.walk() if n.tag == 'annotation' and 'tex' in n.attrs.get('encoding', '').lower()), None)
        return '$' + (latex or ''.join(text(c) for c in node.children)) + '$'
    values = [text(c) for c in node.children]
    if node.tag in {'msup', 'msub', 'msubsup', 'mfrac'}:
        values = [v for v in values if v.strip()]
        if node.tag == 'mfrac' and len(values) == 2:
            return r'\frac{' + values[0] + '}{' + values[1] + '}'
        if len(values) >= 2:
            result = '{' + values[0] + '}' + ('^' if node.tag == 'msup' else '_') + '{' + values[1] + '}'
            return result + ('^{' + values[2] + '}' if node.tag == 'msubsup' and len(values) > 2 else '')
    if node.tag == 'semantics':
        return next((text(c) for c in node.children if isinstance(c, Node) and c.tag != 'annotation'), '')
    if node.tag == 'img':
        return node.attrs.get('alt', '')
    value = ''.join(values)
    if node.tag in {'sup', 'sub'}:
        return ('^' if node.tag == 'sup' else '_') + '{' + value + '}'
    if node.tag in {'p', 'div', 'section', 'article', 'main', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr', 'blockquote', 'pre', 'br', 'hr', 'dt', 'dd'}:
        anchor = node.attrs.get('id', '')
        return '\n' + (f'[#{anchor}] ' if anchor else '') + value + '\n'
    if node.tag in {'td', 'th'}:
        return value + ' | '
    return value


def text_of(node):
    if isinstance(node, str):
        return node
    rendered = {}

    def text(child):
        return child if isinstance(child, str) else rendered[id(child)]

    for current in _post_order(node):
        rendered[id(current)] = _render(current, text)
    return rendered[id(node)]


def _metadata_region(node):
    labels = re.split(r'[\s_-]+', (node.attrs.get('class', '') + ' ' + node.attrs.get('id', '')).lower())
    return (node.tag in {'nav', 'header', 'footer', 'aside', 'form'}
            or bool(set(labels) & {'abstract', 'abstracts', 'toc', 'contents', 'bibliography', 'references', 'ref-list'})
            or node.attrs.get('role') in {'doc-abstract', 'doc-toc', 'doc-bibliography'})


def _kept_children(node, excluded):
    children, skip_level = [], None
    for child in node.children:
        if isinstance(child, Node) and re.fullmatch(r'h[1-6]', child.tag):
            level = int(child.tag[1])
            heading = re.sub(r'\s+', ' ', text_of(child)).strip().lower()
            heading = re.sub(r'^\[#.*?\]\s*', '', heading)
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if re.fullmatch(r'(abstract|table of contents|contents|references|bibliography|article information|metrics)', heading):
                excluded.add('metadata_heading_section')
                skip_level = level
        if skip_level is None:
            children.append(child)
    return children


def body_only(node, excluded):
    """Keep article prose, excluding metadata even when it contains math/keywords.

    Heading-only abstract/TOC sections are common on older publisher pages, so
    class names alone are insufficient. Preserve the original tree for receipts.
    """
    if isinstance(node, str):
        return node
    kept, built, stack = {}, {}, [node]
    # Decide each kept subtree top-down, then rebuild bottom-up without recursion.
    while stack:
        current = stack.pop()
        if _metadata_region(current):
            excluded.add('navigation_or_metadata_region')
            built[id(current)] = Node('div')
            continue
        kept[id(current)] = (current, _kept_children(current, excluded))
        stack.extend(child for child in kept[id(current)][1] if isinstance(child, Node))
    for current, children in reversed(list(kept.values())):
        built[id(current)] = Node(current.tag, current.attrs,
                                  [child if isinstance(child, str) else built[id(child)] for child in children])
    return built[id(node)]


def extract_html(raw: str, url: str) -> dict:
    parser = Document()
    parser.feed(raw)
    nodes = list(parser.root.walk())
    metadata = {}
    candidates = []
    title = next((text_of(n).strip() for n in nodes if n.tag == 'title'), '')
    for n in nodes:
        a = n.attrs
        if n.tag == 'meta':
            key = a.get('name', a.get('property', '')).lower()
            metadata.setdefault(key, []).append(a.get('content', ''))
            if key in {'citation_pdf_url', 'eprints.document_url', 'wkhealth_pdf_url'}:
                candidates.append(a.get('content', ''))
        if n.tag == 'link' and a.get('type', '').lower() in {'application/pdf', 'application/x-tex'}:
            candidates.append(a.get('href', ''))
    # Download controls, not arbitrary PDFs cited in the bibliography.
    for n in nodes:
        if n.tag != 'a':
            continue
        label = re.sub(r'\s+', ' ', text_of(n)).strip().lower().strip('[]() ')
        href = n.attrs.get('href', '')
        if (re.search(r'^(download( (this|the))?( (article|paper|full.text))?( \(?pdf\)?)?|pdf|full[ -]?text( pdf)?|view .* as pdf)$', label)
                or 'download' in n.attrs or n.attrs.get('type') == 'application/pdf'
                or ('pdf' in label and re.search(r'\.pdf(?:[?#]|$)', href, re.I) and len(label) < 90)):
            candidates.append(href)
    for n in nodes:
        if n.tag in {'iframe', 'embed', 'object'}:
            embedded = n.attrs.get('src', n.attrs.get('data', ''))
            if re.search(r'\.pdf(?:[?#]|$)', embedded, re.I):
                candidates.append(embedded)
    parts = urlsplit(url)
    if parts.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'} and parts.path.startswith(('/abs/', '/html/')):
        candidates.insert(0, 'https://arxiv.org/pdf/' + parts.path.split('/', 2)[2])
    candidates = list(dict.fromkeys(urljoin(url, c) for c in candidates if c and urlsplit(urljoin(url, c)).scheme in {'http', 'https'}))
    if parts.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'} and parts.path.startswith(('/abs/', '/html/')):
        paper_id = parts.path.split('/', 2)[2]
        if re.search(r'v\d+$', paper_id):
            # arXiv's citation_pdf_url can point to the latest, unversioned PDF
            # even on a versioned abstract page. Never silently change versions.
            candidates = [c for c in candidates if urlsplit(c).hostname not in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}
                          or urlsplit(c).path == '/pdf/' + paper_id]
    candidates = candidates[:4]
    main = next((n for n in nodes if n.tag == 'main'), None)
    if main is None:
        main = next((n for n in nodes if n.tag == 'article'), None)
    if main is None:
        main = next((n for n in nodes if set(n.attrs.get('class', '').split()) & {'entry-content', 'entry', 'main-text', 'post-content', 'ltx_document'}), parser.root)
    text = re.sub(r'[ \t]+', ' ', text_of(main))
    text = re.sub(r'\n\s*\n+', '\n\n', text).strip()
    page_text = re.sub(r'\s+', ' ', text).lower()
    challenge = bool(re.search(r'access denied|just a moment|attention required|pardon our interruption|verify you are human|robot check|sign in to continue', title.lower()))
    challenge |= len(text) < 2000 and any(s in page_text for s in ('enable javascript and cookies', 'checking your browser', 'complete the security check'))
    body_classes = {'ltx_document', 'article-body', 'article_body', 'fulltext-body'}
    explicit_body = next((n for n in nodes if set(n.attrs.get('class', '').split()) & body_classes), None)
    product_page = any('productdetailmainwrapper' == n.attrs.get('id', '').lower()
                       or n.attrs.get('itemtype', '').rstrip('/').endswith('/Product') for n in nodes)
    catalogue = any(n.attrs.get('id', '').lower() == 'issuetabcontent' for n in nodes)
    catalogue |= bool(re.search(r'\bcontents of (?:volume|issue)\b', page_text))
    excluded = set()
    body = body_only(explicit_body or main, excluded)
    body_text = re.sub(r'\s+', ' ', text_of(body)).strip()
    full_text = body_text.lower()
    link_chars = sum(len(text_of(n)) for n in body.walk() if n.tag == 'a')
    link_heavy = link_chars > len(body_text) * 0.5
    scholarly_host = parts.hostname in {'stacks.math.columbia.edu', 'dlmf.nist.gov'} or (parts.hostname in {'arxiv.org', 'www.arxiv.org'} and parts.path.startswith('/html/'))
    classes = ' '.join(n.attrs.get('class', '') for n in nodes)
    substantive = bool(re.search(r'\b(theorem|lemma|proposition|proof|definition|corollary|methods|results)\b', full_text))
    sections = sum(bool(re.search(r'\b' + word + r'\b', full_text)) for word in ('introduction', 'proof', 'theorem', 'methods', 'discussion', 'conclusion'))
    full_article = len(body_text) > 1200 and substantive and sections >= 2 and (main is not parser.root or 'ltx_document' in classes or 'entry-content' in classes or 'article-body' in classes)
    math_count = sum(n.tag == 'math' or n.tag == 'script' and 'math/tex' in n.attrs.get('type', '') or n.tag == 'img' and ('$' in n.attrs.get('alt', '') or 'latex' in n.attrs.get('class', '').lower()) for n in body.walk())
    math_count += body_text.count('\\(') + body_text.count('\\[') + body_text.count('$') // 2
    has_heading = any(n.tag in {'h1', 'h2', 'h3'} for n in nodes)
    legacy_math = len(body_text) > 500 and substantive and math_count >= 3 and has_heading
    metadata_page = ((product_page or catalogue) and explicit_body is None) or link_heavy
    ready = not challenge and not metadata_page and ((scholarly_host and len(body_text) > 200 and substantive) or full_article or legacy_math)
    # An abstract landing page with a PDF control is navigation, not full text.
    if parts.path.startswith('/abs/') or '/article/abs/' in parts.path:
        ready = False
    if catalogue and explicit_body is None:
        # Issue-page PDF controls refer to multiple unrelated works. Do not
        # silently replace a failed article URL with the first current article.
        candidates = []
    return {'title': title, 'text': text, 'metadata': metadata, 'candidates': candidates,
            'kind': 'challenge' if challenge else 'fulltext' if ready else 'landing_page',
            'classification': {'reason': 'access_challenge' if challenge else 'catalogue_or_product_page' if metadata_page else 'body_evidence' if ready else 'insufficient_body_evidence',
                               'body_char_count': len(body_text), 'excluded_regions': sorted(excluded)},
            'math_extraction': 'TeX annotations/alttext preferred; structural MathML fallback; inspect original for ambiguous notation'}
