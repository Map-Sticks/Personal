"""Self-contained SVG room maps and tables for the pyRevit output window."""
from xml.sax.saxutils import escape
from fire_ews_geometry import distance, overlap

COLORS = {'detector': '#cf423a', 'speaker': '#285bcd', 'obstacle': '#777777'}
try:
    text_type = unicode
except NameError:
    text_type = str


def _map(room, report):
    x0, y0, x1, y1 = room['box']
    sx, sy = x1-x0, y1-y0
    scale = min(680.0/max(1, sx), 340.0/max(1, sy))
    width, height = sx*scale+24, sy*scale+24

    def pt(p):
        return (12+(p[0]-x0)*scale, 12+(y1-p[1])*scale)

    def line(a, b, color, size, dash=''):
        start, end = pt(a), pt(b)
        return '<path d="M%.2f %.2fL%.2f %.2f" stroke="%s" stroke-width="%s" %s/>' % (
            start+end+(color, size, dash))

    def path(loops):
        return ' '.join('M'+'L'.join('%.2f %.2f' % pt(p) for p in loop)+'Z' for loop in loops)

    room_path = path(room['loops'])
    result = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
              'viewBox="0 0 %.4f %.4f" role="img" aria-label="Room coordination plan">' %
              (width, height, width, height)]
    clip = 'fews{}'.format(room['id'])
    result.append('<defs><clipPath id="%s"><path d="%s" clip-rule="evenodd"/></clipPath></defs>'
                  '<path d="%s" fill="#f3f5f7" fill-rule="evenodd"/><g clip-path="url(#%s)">' %
                  (clip, room_path, room_path, clip))
    for c in report['ceilings']:
        if c['loops']:
            result.append('<path d="%s" fill="none" stroke="#28816d" stroke-width="1.5"/>' % path(c['loops']))
        for a, b in c['bars']:
            if overlap((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), room['box']):
                result.append(line(a, b, '#98a3b2', 0.75))
    result.append('</g>')
    if room.get('ceiling_zone'):
        result.append('<g clip-path="url(#%s)">' % clip)
    for a, b in room['walls']:
        result.append(line(a, b, '#253040', 2))
    if room.get('ceiling_zone'):
        result.append('</g>')
    for a, b in room['open_edges']:
        result.append(line(a, b, '#b36a12', 1.5, 'stroke-dasharray="4 2"'))
    for d in report['devices']:
        x, y = pt(d['point'])
        result.append('<circle cx="%.2f" cy="%.2f" r="%s" fill="%s"><title>%s %s</title></circle>' %
                      (x, y, 2 if d['kind'] == 'obstacle' else 4, COLORS[d['kind']], d['kind'], d['id']))
    for row in report['proposals']:
        color = COLORS[row['kind']]
        if row['from']:
            result.append(line(row['from'], row['to'], color, 1.5))
        x, y = pt(row['to'])
        result.append('<circle cx="%.2f" cy="%.2f" r="6" fill="none" stroke-width="2" '
                      'stroke="%s"><title>%s</title></circle>' %
                      (x, y, color, 'Move {}'.format(row['id']) if row['id'] else 'Optional new position'))
    result.append('</svg>')
    return ''.join(result)


def _mm(n):
    return '-' if n is None else '{:,.0f}'.format(n)


def _cell(value):
    return '-' if value is None else text_type(value)


def _table(rows, headers):
    return ('<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>'.format(
        ''.join(u'<th>{}</th>'.format(escape(x)) for x in headers),
        ''.join(u'<tr>{}</tr>'.format(''.join(u'<td>{}</td>'.format(escape(_cell(x))) for x in row))
                for row in rows)))


def room_html(room, report):
    box = room['box']
    line = u'{0} {1} | {2:.1f} m2 | extents {3:.1f} x {4:.1f} m'.format(
        room['number'], room['name'], room['area'], (box[2]-box[0])/1000, (box[3]-box[1])/1000)
    html = [u'<details{}><summary><b>{}</b> | {} proposal(s)</summary>'.format(
        ' open' if report['proposals'] else '', escape(line), len(report['proposals']))]
    html.extend('<div>{}</div>'.format(escape(n)) for n in report['notes'])
    html.append(_map(room, report))
    if report['ceilings']:
        html.append(_table([(c['id'], c['name'], c['finish'], _mm(c['z']), len(c['bars']))
                            for c in report['ceilings']], ('Ceiling patch', 'Type', 'Mapping', 'Z mm', 'Grid lines')))
    systems, dev = [], []
    for m in report['metrics']:
        if 'id' in m:
            dev.append((m['id'], m['kind'], m['ceiling'], _mm(m['wall']), m['peer'],
                        _mm(m['pitch']), _mm(m['grid_clear']), m.get('decision'), '; '.join(m['issues'])))
        else:
            systems.append((m['kind'], m['count'], m['sample_count'], m['unseen'], _mm(m['max_reach']),
                            m['missed'], m['edge_count'], _mm(m['edge_reach']), m['edge_missed']))
    html.append(_table(systems, ('System', 'Count', 'Interior samples', 'No direct ray', 'Max interior mm',
                                 'Interior over limit', 'Wall samples', 'Max wall reach mm', 'Wall over limit')))
    if dev:
        html.append(_table(dev, ('ID', 'System', 'Ceiling', 'Nearest wall mm', 'Neighbour ID',
                                 'Nearest spacing mm', 'Nearest T-bar mm', 'Move assessment', 'Notes')))
    if report['proposals']:
        def xy(p):
            return ', '.join(_mm(x) for x in p) if p else '-'
        html.append(_table([(r['id'] if r['id'] is not None else 'NEW OPTION', r['kind'], xy(r['from']),
                            xy(r['to']), _mm(distance(r['from'], r['to'])) if r['from'] else '-',
                            _mm(r.get('wall')), _mm(r.get('bar')), r['ceiling'], r['reason'])
                           for r in report['proposals']],
                           ('Device', 'System', 'From XY mm', 'To XY mm', 'Move mm',
                            'Target to wall mm', 'Target to T-bar mm', 'Ceiling', 'Reason')))
    html.append('</details>')
    return ''.join(html)
