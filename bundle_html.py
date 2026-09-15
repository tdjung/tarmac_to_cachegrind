#!/usr/bin/env python3
"""Embed local screenshots and replace named links in HTML (Python 3.6.8+)."""
import argparse
import base64
import html
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit


class Slots(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=False)
        self.document = document
        self.offsets = [0]
        for line in document.splitlines(True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for marker, target, expected in [('data-image', 'src', 'img'), ('data-link', 'href', 'a')]:
            if marker in attrs:
                if tag != expected or not attrs[marker]:
                    raise ValueError('{} requires a nonempty key on <{}>'.format(marker, expected))
                line, column = self.getpos()
                start = self.offsets[line - 1] + column
                self.tags.append((start, self.get_starttag_text(), marker, target, attrs[marker]))

    handle_startendtag = handle_starttag


def assignments(values):
    result = {}
    for value in values or []:
        key, separator, item = value.partition('=')
        if not separator or not key or not item or key in result:
            raise ValueError('expected a unique KEY=VALUE: ' + value)
        result[key] = item
    return result


def image_uri(filename):
    data = Path(filename).expanduser().read_bytes()
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('expected PNG, JPEG, GIF or WebP image: ' + filename)
    return 'data:{};base64,{}'.format(mime, base64.b64encode(data).decode('ascii'))


def link_value(value):
    if any(ord(c) < 32 for c in value) or urlsplit(value).scheme.lower() not in ('', 'http', 'https', 'file'):
        raise ValueError('link must be HTTP(S), file URI, or relative path')
    return value


def replace_attribute(raw, name, value):
    # Match complete attributes so text inside another quoted attribute is untouched.
    pattern = re.compile(r'''([^\s=<>/]+)(\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)''')
    found = [False]
    def change(match):
        if match.group(1).lower() != name:
            return match.group(0)
        if found[0]:
            raise ValueError('duplicate ' + name + ' attribute')
        found[0] = True
        return name + '="' + html.escape(value, quote=True) + '"'
    result = pattern.sub(change, raw)
    if not found[0]:
        at = result.rfind('/>') if result.endswith('/>') else len(result) - 1
        result = result[:at] + ' ' + name + '="' + html.escape(value, quote=True) + '"' + result[at:]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='HTML template containing data-image/data-link attributes')
    parser.add_argument('-o', '--output', type=Path, help='destination HTML (may equal input)')
    parser.add_argument('--image', action='append', metavar='KEY=PATH', help='embed a local image; repeatable')
    parser.add_argument('--link', action='append', metavar='KEY=URL', help='replace a named link; repeatable')
    parser.add_argument('--github-url', help='shortcut for --link github=URL')
    parser.add_argument('--readme-url', help='shortcut for --link readme=URL; independent of GitHub URL')
    parser.add_argument('--profiler-url', help='shortcut for --link profiler=URL')
    parser.add_argument('--list-slots', action='store_true', help='show available image/link keys')
    args = parser.parse_args(argv)
    try:
        document = args.input.read_text(encoding='utf-8')
        slots = Slots(document)
        images, links = assignments(args.image), assignments(args.link)
        for key, value in [('github', args.github_url), ('readme', args.readme_url), ('profiler', args.profiler_url)]:
            if value is not None:
                if key in links:
                    raise ValueError('link specified twice: ' + key)
                links[key] = value
        known = {marker: {t[4] for t in slots.tags if t[2] == marker} for marker in ('data-image', 'data-link')}
        if args.list_slots:
            for marker in ('data-image', 'data-link'):
                print('{}: {}'.format(marker, ', '.join(sorted(known[marker]))))
        if not args.output:
            if args.list_slots and not images and not links:
                return 0
            raise ValueError('-o/--output is required')
        for marker, values in [('data-image', images), ('data-link', links)]:
            unknown = set(values) - known[marker]
            if unknown:
                raise ValueError('unknown {} keys: {}'.format(marker, ', '.join(sorted(unknown))))
        replacements = {'data-image': {k: image_uri(v) for k, v in images.items()},
                        'data-link': {k: link_value(v) for k, v in links.items()}}
        # Complete validation before replacing the output, including in-place edits.
        for start, raw, marker, target, key in reversed(slots.tags):
            if key in replacements[marker]:
                updated = replace_attribute(raw, target, replacements[marker][key])
                document = document[:start] + updated + document[start + len(raw):]
        destination = args.output.resolve()
        for image_path in images.values():
            source = Path(image_path).expanduser().resolve()
            if destination == source or (destination.exists() and source.exists() and destination.samefile(source)):
                raise ValueError('output must not overwrite an input image')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(destination.parent), delete=False) as out:
                temporary = out.name
                out.write(document)
            os.replace(temporary, str(destination))
            temporary = None
        finally:
            if temporary:
                os.unlink(temporary)
        print('complete "{}"'.format(args.output))
        return 0
    except (OSError, UnicodeError, ValueError) as exc:
        print('error: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
