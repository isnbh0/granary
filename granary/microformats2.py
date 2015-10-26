"""Convert ActivityStreams to microformats2 HTML and JSON.

Microformats2 specs: http://microformats.org/wiki/microformats2
"""

from collections import deque
import copy
import itertools
import urlparse
import string
import xml.sax.saxutils

from mf2py import parser
from oauth_dropins.webutil import util
import source

HENTRY = string.Template("""\
<article class="$types">
  <span class="p-uid">$uid</span>
  $summary
  $published
  $updated
$author
  $linked_name
  <div class="$content_classes">
  $invitees
  $content
  </div>
$video
$photo
$location
$in_reply_tos
$likes_and_reposts
$comments
</article>
""")
HCARD = string.Template("""\
  <span class="$types">
    $linked_name
    $photo
  </span>
""")
IN_REPLY_TO = string.Template('  <a class="u-in-reply-to" href="$url"></a>')
# Simplified version of a nested post, for use in the outer post's content
HCITE_AS_CONTENT = string.Template("""\
$participle $linked_name by $author
<div>
  $content
</div>
""")


def get_string_urls(objs):
  """Extracts string URLs from a list of either string URLs or mf2 dicts.

  Many mf2 properties can contain either string URLs or full mf2 objects, e.g.
  h-cites. in-reply-to is the most commenly used example:
  http://indiewebcamp.com/in-reply-to#How_to_consume_in-reply-to

  Args:
    objs: sequence of either string URLs or embedded mf2 objects

  Returns: list of string URLs
  """
  urls = []
  for item in objs:
    if isinstance(item, basestring):
      urls.append(item)
    else:
      itemtype = [x for x in item.get('type', []) if x.startswith('h-')]
      if itemtype:
        item = item.get('properties') or item
        urls.extend(get_string_urls(item.get('url', [])))

  return urls


def get_html(val):
  """Returns a string value that may have HTML markup.

  Args:
    value: mf2 property value, either string or
     {'html': '<p>str</p>', 'value': 'str'} dict

  Returns: string or None
  """
  return val.get('html') or val.get('value') if isinstance(val, dict) else val


def get_text(val):
  """Returns a plain text string value. See get_html."""
  return val.get('value') if isinstance(val, dict) else val


def object_to_json(obj, trim_nulls=True, entry_class='h-entry',
                   default_object_type=None):
  """Converts an ActivityStreams object to microformats2 JSON.

  Args:
    obj: dict, a decoded JSON ActivityStreams object
    trim_nulls: boolean, whether to remove elements with null or empty values
    entry_class: string, the mf2 class that entries should be given (e.g.
      'h-cite' when parsing a reference to a foreign entry). defaults to
      'h-entry'
    default_object_type: string, the ActivityStreams objectType to use if one
      is not present. defaults to None

  Returns: dict, decoded microformats2 JSON
  """
  if not obj:
    return {}

  types_map = {'article': [entry_class, 'h-as-article'],
               'comment': [entry_class, 'h-as-comment'],
               'like': [entry_class, 'h-as-like'],
               'note': [entry_class, 'h-as-note'],
               'person': ['h-card'],
               'place': ['h-card', 'h-as-location'],
               'share': [entry_class, 'h-as-share'],
               'rsvp-yes': [entry_class, 'h-as-rsvp'],
               'rsvp-no': [entry_class, 'h-as-rsvp'],
               'rsvp-maybe': [entry_class, 'h-as-rsvp'],
               'invite': [entry_class],
               }
  obj_type = source.object_type(obj) or default_object_type
  # if the activity type is a post, then it's really just a conduit
  # for the object. for other verbs, the activity itself is the
  # interesting thing
  if obj_type == 'post':
    primary = obj.get('object', {})
    obj_type = source.object_type(primary) or default_object_type
  else:
    primary = obj

  types = types_map.get(obj_type, [entry_class])

  url = obj.get('url', primary.get('url', ''))
  content = primary.get('content', '')

  # TODO: extract snippet
  name = primary.get('displayName', primary.get('title'))
  summary = primary.get('summary')
  author = obj.get('author', obj.get('actor', {}))

  in_reply_tos = obj.get(
    'inReplyTo', obj.get('context', {}).get('inReplyTo', []))
  if 'h-as-rsvp' in types and 'object' in obj:
    in_reply_tos.append(obj['object'])
  # TODO: more tags. most will be p-category?
  ret = {
    'type': types,
    'properties': {
      'uid': [obj.get('id', '')],
      'name': [name],
      'summary': [summary],
      'url': [url] + obj.get('upstreamDuplicates', []),
      'photo': [obj.get('image', primary.get('image', {})).get('url', '')],
      'video': [obj.get('stream', primary.get('stream', {})).get('url')],
      'published': [obj.get('published', primary.get('published', ''))],
      'updated': [obj.get('updated', primary.get('updated', ''))],
      'content': [{
          'value': xml.sax.saxutils.unescape(content),
          'html': render_content(primary, include_location=False),
      }],
      'in-reply-to': util.trim_nulls([o.get('url') for o in in_reply_tos]),
      'author': [object_to_json(
        author, trim_nulls=False, default_object_type='person')],
      'location': [object_to_json(
        primary.get('location', {}), trim_nulls=False,
        default_object_type='place')],
      'comment': [object_to_json(c, trim_nulls=False, entry_class='h-cite')
                  for c in obj.get('replies', {}).get('items', [])],
      }
    }

  # rsvp
  if 'h-as-rsvp' in types:
    ret['properties']['rsvp'] = [obj_type[len('rsvp-'):]]
  elif obj_type == 'invite':
    invitee = object_to_json(obj.get('object'), trim_nulls=False,
                             default_object_type='person')
    ret['properties']['invitee'] = [invitee]

  # like and repost mentions
  for type, prop in ('like', 'like'), ('share', 'repost'):
    if obj_type == type:
      # The ActivityStreams spec says the object property should always be a
      # single object, but it's useful to let it be a list, e.g. when a like has
      # multiple targets, e.g. a like of a post with original post URLs in it,
      # which brid.gy does.
      objs = obj.get('object', [])
      if not isinstance(objs, list):
        objs = [objs]
      ret['properties'][prop + '-of'] = ret['properties'][prop] = [
        o['url'] if 'url' in o and set(o.keys()) <= set(['url', 'objectType'])
        else object_to_json(o, trim_nulls=False, entry_class='h-cite')
        for o in objs]
    else:
      # received likes and reposts
      ret['properties'][prop] = [
        object_to_json(t, trim_nulls=False, entry_class='h-cite')
        for t in obj.get('tags', []) if source.object_type(t) == type]

  if trim_nulls:
    ret = util.trim_nulls(ret)
  return ret


def json_to_object(mf2):
  """Converts microformats2 JSON to an ActivityStreams object.

  Args:
    mf2: dict, decoded JSON microformats2 object

  Returns: dict, ActivityStreams object
  """
  if not mf2 or not isinstance(mf2, dict):
    return {}

  props = mf2.get('properties', {})
  prop = first_props(props)
  rsvp = prop.get('rsvp')
  rsvp_verb = 'rsvp-%s' % rsvp if rsvp else None
  author = json_to_object(prop.get('author'))

  # maps mf2 type to ActivityStreams objectType and optional verb. ordered by
  # priority.
  types = mf2.get('type', [])
  types_map = [
    ('h-as-rsvp', 'activity', rsvp_verb),
    ('h-as-share', 'activity', 'share'),
    ('h-as-like', 'activity', 'like'),
    ('h-as-comment', 'comment', None),
    ('h-as-reply', 'comment', None),
    ('h-as-location', 'place', None),
    ('h-card', 'person', None),
    ]

  # fallback if none of the above mf2 types are found. maps property (if it
  # exists) to objectType and verb. ordered by priority.
  prop_types_map = [
    ('rsvp', 'activity', rsvp_verb),
    ('invitee', 'activity', 'invite'),
    ('repost-of', 'activity', 'share'),
    ('like-of', 'activity', 'like'),
    ('in-reply-to', 'comment', None),
    ]

  for mf2_type, as_type, as_verb in types_map:
    if mf2_type in types:
      break  # found
  else:
    for p, as_type, as_verb in prop_types_map:
      if p in props:
        break
    else:
      # default
      as_type = 'note' if 'h-as-note' in types else 'article'
      as_verb = None

  photos = [url for url in get_string_urls(props.get('photo', []))
            # filter out relative and invalid URLs (mf2py gives absolute urls)
            if urlparse.urlparse(url).netloc]

  urls = props.get('url') and get_string_urls(props.get('url'))

  obj = {
    'id': prop.get('uid'),
    'objectType': as_type,
    'verb': as_verb,
    'published': prop.get('published', ''),
    'updated': prop.get('updated', ''),
    'displayName': get_text(prop.get('name')),
    'summary': get_text(prop.get('summary')),
    'content': get_html(prop.get('content')),
    'url': urls[0] if urls else None,
    'image': {'url': photos[0] if photos else None},
    'location': json_to_object(prop.get('location')),
    'replies': {'items': [json_to_object(c) for c in props.get('comment', [])]},
    }

  if as_type == 'activity':
    objects = []
    for target in itertools.chain.from_iterable(
        props.get(field, []) for field in (
          'like', 'like-of', 'repost', 'repost-of', 'in-reply-to', 'invitee')):
      t = json_to_object(target) if isinstance(target, dict) else {'url': target}
      # eliminate duplicates from redundant backcompat properties
      if t not in objects:
        objects.append(t)
    obj.update({
        'object': objects[0] if len(objects) == 1 else objects,
        'actor': author,
        })
  else:
    obj.update({
        'inReplyTo': [{'url': url} for url in get_string_urls(props.get('in-reply-to', []))],
        'author': author,
        })

  return util.trim_nulls(obj)


def html_to_activities(html, url=None):
  """Converts a microformats2 HTML h-feed to ActivityStreams activities.

  Args:
    html: string HTML
    url: optional string URL that HTML came from

  Returns: list of ActivityStreams activity dicts
  """
  parsed = parser.Parser(doc=html, url=None).to_dict()
  hfeed = find_first_entry(parsed, ['h-feed'])
  items = hfeed.get('children', []) if hfeed else parsed.get('items', [])
  return [{'object': json_to_object(item)} for item in items]


def activities_to_html(activities):
  """Converts ActivityStreams activities to a microformats2 HTML h-feed.

  Args:
    obj: dict, a decoded JSON ActivityStreams object

  Returns: string, the content field in obj with the tags in the tags field
    converted to links if they have startIndex and length, otherwise added to
    the end.
  """
  return """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
%s
</body>
</html>
  """ % '\n'.join(object_to_html(a) for a in activities)


def object_to_html(obj, parent_props=[]):
  """Converts an ActivityStreams object to microformats2 HTML.

  Features:
  - linkifies embedded tags and adds links for other tags
  - linkifies embedded URLs
  - adds links, summaries, and thumbnails for attachments and checkins
  - adds a "via SOURCE" postscript

  Args:
    obj: dict, a decoded JSON ActivityStreams object
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. ['u-repost-of']

  Returns: string, the content field in obj with the tags in the tags field
    converted to links if they have startIndex and length, otherwise added to
    the end.
  """
  return json_to_html(object_to_json(obj), parent_props)


def json_to_html(obj, parent_props=[]):
  """Converts a microformats2 JSON object to microformats2 HTML.

  See object_to_html for details.

  Args:
    obj: dict, a decoded microformats2 JSON object
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. 'u-repost-of'

  Returns: string HTML
  """
  if not obj:
    return ''

  # TODO: handle when h-card isn't first
  if obj['type'][0] == 'h-card':
    return hcard_to_html(obj, parent_props)

  props = copy.copy(obj['properties'])
  in_reply_tos = '\n'.join(IN_REPLY_TO.substitute(url=url)
                           for url in get_string_urls(props.get('in-reply-to', [])))

  prop = first_props(props)
  prop.setdefault('uid', '')
  author = prop.get('author')

  # if this post is an rsvp, populate its data element. if it's an invite, give
  # it a default name.
  # do this *before* content since it sets props['name'] if necessary.
  rsvp = prop.get('rsvp')
  if rsvp:
    if not props.get('name'):
      props['name'] = [{'yes': 'is attending.',
                        'no': 'is not attending.',
                        'maybe': 'might attend.'}.get(rsvp)]
    props['name'][0] = '<data class="p-rsvp" value="%s">%s</data>' % (
      rsvp, props['name'][0])

  elif props.get('invitee') and not props.get('name'):
    props['name'] = ['invited']

  # if this post is itself a like or repost, link to its target(s).
  likes_and_reposts = []
  # simple version of the like/repost context for the outer post's e-content
  likes_and_reposts_as_content = []

  for astype, mftype, part in [
      ('like', 'like', 'Liked'),
      ('share', 'repost', 'Shared')
  ]:
    # having like-of or repost-of makes this a like or repost.
    for target in props.get(mftype + '-of', []):
      if isinstance(target, dict):
        likes_and_reposts.append(json_to_html(
          target, ['u-' + mftype, 'u-' + mftype + '-of']))
        likes_and_reposts_as_content.append(
          hcite_to_content_html(target, part))
      else:
        # this simple context can go right into the e-content. only
        # include text if this is the first one.
        phrase = ('%ss this.' % mftype) if not likes_and_reposts_as_content else ''
        likes_and_reposts_as_content.append(
          '<a class="u-%s u-%s-of" href="%s">%s</a>' % (
            mftype, mftype, target, phrase))

  # set up content and name
  content = prop.get('content', {})
  content_html = '\n'.join(
    [content.get('html', '') or content.get('value', '')]
    + likes_and_reposts_as_content)
  content_classes = []

  if content_html:
    content_classes.append('e-content')
    if not props.get('name'):
      content_classes.append('p-name')

  summary = ('<div class="p-summary">%s</div>' % prop.get('summary')
             if prop.get('summary') else '')

  photo = '\n'.join(img(url, 'u-photo', 'attachment')
                    for url in props.get('photo', []) if url)
  video = '\n'.join(vid(url, None, 'u-video')
                    for url in props.get('video', []) if url)

  # comments
  # http://indiewebcamp.com/comment-presentation#How_to_markup
  # http://indiewebcamp.com/h-cite
  comments_html = '\n'.join(json_to_html(c, ['p-comment'])
                            for c in props.get('comment', []))

  # embedded likes and reposts of this post
  # http://indiewebcamp.com/like, http://indiewebcamp.com/repost
  for verb in 'like', 'repost':
    # including u-like and u-repost for backcompat means that we must ignore
    # these properties when converting a post that is itself a like or repost
    if not verb + '-of' in props:
      vals = props.get(verb, [])
      if vals and isinstance(vals[0], dict):
        likes_and_reposts += [json_to_html(v, ['u-' + verb]) for v in vals]

  return HENTRY.substitute(
    prop,
    published=maybe_datetime(prop.get('published'), 'dt-published'),
    updated=maybe_datetime(prop.get('updated'), 'dt-updated'),
    types=' '.join(parent_props + obj['type']),
    author=hcard_to_html(author, ['p-author']),
    location=hcard_to_html(prop.get('location'), ['p-location']),
    photo=photo,
    video=video,
    in_reply_tos=in_reply_tos,
    invitees='\n'.join([hcard_to_html(i, ['p-invitee'])
                        for i in props.get('invitee', [])]),
    content=content_html,
    content_classes=' '.join(content_classes),
    comments=comments_html,
    likes_and_reposts='\n'.join(likes_and_reposts),
    linked_name=maybe_linked_name(props),
    summary=summary)


def hcite_to_content_html(hcite, participle):
  """Generate a simplified rendering of an h-cite to use as the
  e-content of the outer post. Somewhat confusingly, this rendering
  *cannot* include mf2 property classes, or those properties would be
  hoisted to the outer h-entry.

  Args:
    hcite: dict, json-mf2 objec for the nested h-cite
    participle: string, the past-tense form of the verb that describes this
      post's relation to its containing post (e.g. "Shared")

  Returns: string, rendered html
  """
  prop = first_props(hcite['properties'])
  content = prop.get('content', {})
  content_html = content.get('html', content.get('value', ''))

  # remove the author photo, looks bad in the simple rendering
  author = copy.deepcopy(prop.get('author', prop.get('actor', {})))
  if 'photo' in author['properties']:
    del author['properties']['photo']

  return HCITE_AS_CONTENT.substitute(
    participle=participle,
    linked_name='<a href="%s">%s</a>' % (
      prop.get('url', '#'), prop.get('name', 'a post')),
    # class="h-card" is ok, but make sure not to give it p-author
    author=hcard_to_html(author),
    content=content_html)


def hcard_to_html(hcard, parent_props=[]):
  """Renders an h-card as HTML.

  Args:
    hcard: dict, decoded JSON h-card
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. ['p-author']

  Returns: string, rendered HTML
  """
  if not hcard:
    return ''

  # extract first value from multiply valued properties
  prop = first_props(hcard['properties'])
  prop.setdefault('uid', '')
  photo = prop.get('photo')
  return HCARD.substitute(
    prop,
    types=' '.join(parent_props + hcard['type']),
    photo=img(photo, 'u-photo', '') if photo else '',
    linked_name=maybe_linked_name(hcard['properties']))


def render_content(obj, include_location=True):
  """Renders the content of an ActivityStreams object.

  Includes tags, mentions, and attachments.

  Args:
    obj: decoded JSON ActivityStreams object
    include_location: whether to render location, if provided

  Returns: string, rendered HTML
  """
  content = obj.get('content', '')

  # extract tags. preserve order but de-dupe, ie don't include a tag more than
  # once.
  seen_ids = set()
  mentions = []
  tags = {}  # maps string objectType to list of tag objects
  for t in obj.get('tags', []):
    id = t.get('id')
    if id and id in seen_ids:
      continue
    seen_ids.add(id)

    if 'startIndex' in t and 'length' in t:
      mentions.append(t)
    else:
      tags.setdefault(source.object_type(t), []).append(t)

  # linkify embedded mention tags inside content.
  if mentions:
    mentions.sort(key=lambda t: t['startIndex'])
    last_end = 0
    orig = content
    content = ''
    for tag in mentions:
      start = tag['startIndex']
      end = start + tag['length']
      content += orig[last_end:start]
      content += '<a href="%s">%s</a>' % (
        tag['url'], orig[start:end])
      last_end = end

    content += orig[last_end:]

  # convert newlines to <br>s
  # do this *after* linkifying tags so we don't have to shuffle indices over
  content = content.replace('\n', '<br />\n')

  # linkify embedded links. ignore the "mention" tags that we added ourselves.
  # TODO: fix the bug in test_linkify_broken() in webutil/util_test.py, then
  # uncomment this.
  # if content:
  #   content = util.linkify(content)

  # attachments, e.g. links (aka articles)
  # TODO: use oEmbed? http://oembed.com/ , http://code.google.com/p/python-oembed/
  for tag in obj.get('attachments', []) + tags.pop('article', []):
    name = tag.get('displayName', '')
    open_a_tag = False
    if tag.get('objectType') == 'video':
      video = tag.get('stream') or obj.get('stream')
      if video:
        if isinstance(video, list):
          video = video[0]
        poster = tag.get('image', {})
        if poster and isinstance(poster, list):
          poster = poster[0]
        if video.get('url'):
          content += '\n<p>%s</p>' % vid(
            video['url'], poster.get('url'), 'thumbnail')
    else:
      content += '\n<p>'
      url = tag.get('url') or obj.get('url')
      if url:
        content += '\n<a class="link" href="%s">' % url
        open_a_tag = True
      image = tag.get('image') or obj.get('image')
      if image:
        if isinstance(image, list):
          image = image[0]
        if image.get('url'):
          content += '\n' + img(image['url'], 'thumbnail', name)
    if name:
      content += '\n<span class="name">%s</span>' % name
    if open_a_tag:
      content += '\n</a>'
    summary = tag.get('summary')
    if summary and summary != name:
      content += '\n<span class="summary">%s</span>' % summary
    content += '\n</p>'

  # location
  loc = obj.get('location')
  if include_location and loc:
    content += '\n' + hcard_to_html(
      object_to_json(loc, default_object_type='place'),
      parent_props=['p-location'])

  # other tags, except likes and (re)shares. they're rendered manually in
  # json_to_html().
  tags.pop('like', [])
  tags.pop('share', [])
  content += tags_to_html(tags.pop('hashtag', []), 'p-category')
  content += tags_to_html(tags.pop('mention', []), 'u-mention')
  content += tags_to_html(sum(tags.values(), []), 'tag')

  return content


def first_props(props):
  """Converts a multiply-valued dict to singly valued.

  Args:
    props: dict of properties, where each value is a sequence

  Returns: corresponding dict with just the first value of each sequence, or ''
    if the sequence is empty
  """
  if not props:
    return {}

  prop = {}
  for k, v in props.items():
    if not v:
      prop[k] = ''
    elif isinstance(v, (tuple, list)):
      prop[k] = v[0]
    else:
      prop[k] = v

  return prop


def tags_to_html(tags, classname):
  """Returns an HTML string with links to the given tag objects.

  Args:
    tags: decoded JSON ActivityStreams objects.
    classname: class for span to enclose tags in
  """
  return ''.join('\n<a class="%s" href="%s">%s</a>' % (classname, t['url'],
                                                       t.get('displayName', ''))
                 for t in tags if t.get('url'))


def author_display_name(hcard):
  """Returns a human-readable string display name for an h-card object."""
  name = None
  if hcard:
    prop = first_props(hcard.get('properties'))
    name = prop.get('name') or prop.get('uid')
  return name if name else 'Unknown'


def maybe_linked_name(props):
  """Returns the HTML for a p-name with an optional u-url inside.

  Args:
    props: *multiply-valued* properties dict

  Returns: string HTML
  """
  prop = first_props(props)
  name = prop.get('name')
  url = prop.get('url')

  if name:
    html = maybe_linked(name, url, linked_classname='p-name u-url',
                        unlinked_classname='p-name')
  else:
    html = maybe_linked(url or '', url, linked_classname='u-url')

  extra_urls = props.get('url', [])[1:]
  if extra_urls:
    html += '\n' + '\n'.join(maybe_linked('', url, linked_classname='u-url')
                             for url in extra_urls)

  return html


def img(src, cls, alt):
  """Returns an <img> string with the given src, class, and alt.

  Args:
    src: string, url of the image
    cls: string, css class applied to the img tag
    alt: string, alt attribute value, or None

  Returns: string
  """
  return '<img class="%s" src="%s" alt=%s />' % (
      cls, src, xml.sax.saxutils.quoteattr(alt or ''))


def vid(src, poster, cls):
  """Returns an <video> string with the given src and class

  Args:
    src: string, url of the video
    poster: sring, optional. url of the poster or preview image
    cls: string, css class applied to the video tag

  Returns: string
  """
  html = '<video class="%s" src="%s"' % (cls, src)
  if poster:
    html += ' poster="%s"' % poster
  html += ' controls>'

  html += 'Your browser does not support the video tag. '
  html += '<a href="%s">Click here to view directly' % src
  if poster:
    html += '<img src="%s"/>' % poster
  html += '</a></video>'
  return html


def maybe_linked(text, url, linked_classname=None, unlinked_classname=None):
  """Wraps text in an <a href=...> iff a non-empty url is provided.

  Args:
    text: string
    url: string or None
    linked_classname: string, optional class attribute to use if url
    unlinked_classname: string, optional class attribute to use if not url

  Returns: string
  """
  if url:
    classname = ' class="%s"' % linked_classname if linked_classname else ''
    return '<a%s href="%s">%s</a>' % (classname, url, text)
  if unlinked_classname:
    return '<span class="%s">%s</span>' % (unlinked_classname, text)
  return text


def maybe_datetime(str, classname):
  """Returns a <time datetime=...> elem if str is non-empty.

  Args:
    str: string RFC339 datetime or None
    classname: string class name

  Returns: string
  """
  if str:
    return '<time class="%s" datetime="%s">%s</time>' % (classname, str, str)
  else:
    return ''


def find_first_entry(parsed, types):
  """Find the first interesting h-* object in BFS-order

  Flagrantly stolen from https://github.com/kylewm/mf2util.
  TODO: bite the bullet, add it as a dependency, and use it from there!
  """
  queue = deque(item for item in parsed['items'])
  while queue:
    item = queue.popleft()
    if any(h_class in item['type'] for h_class in types):
      return item
    queue.extend(item.get('children', []))
