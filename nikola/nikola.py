# -*- coding: utf-8 -*-

import codecs
from collections import defaultdict
from copy import copy
import cPickle
import datetime
import glob
import hashlib
import json
import os
from StringIO import StringIO
import sys
import tempfile
import urllib2
import urlparse

from doit.tools import PythonInteractiveAction, run_once

import nikola
from tools import utils

__all__ = ['Nikola', 'nikola_main']


# config_changed is basically a copy of
# doit's but using pickle instead of trying to serialize manually
class config_changed(object):

    def __init__(self, config):
        self.config = config

    def __call__(self, task, values):
        config_digest = None
        if isinstance(self.config, basestring):
            config_digest = self.config
        elif isinstance(self.config, dict):
            data = cPickle.dumps(self.config)
            config_digest = hashlib.md5(data).hexdigest()
        else:
            raise Exception(('Invalid type of config_changed parameter got %s' +
                             ', must be string or dict') % (type(self.config),))

        def _save_config():
            return {'_config_changed': config_digest}

        task.insert_action(_save_config)
        last_success = values.get('_config_changed')
        if last_success is None:
            return False
        return (last_success == config_digest)

class Post(object):

    """Represents a blog post or web page."""

    def __init__(self, source_path, destination, use_in_feeds,
        translations, default_lang, blog_url, compile_html):
        """Initialize post.

        The base path is the .txt post file. From it we calculate
        the meta file, as well as any translations available, and
        the .html fragment file path.

        `compile_html` is a function that knows how to compile this Post to
        html.
        """
        self.use_in_feeds = use_in_feeds
        self.blog_url = blog_url
        self.source_path = source_path  # posts/blah.txt
        self.post_name = os.path.splitext(source_path)[0]  # posts/blah
        self.base_path = self.post_name + ".html"  # posts/blah.html
        self.metadata_path = self.post_name + ".meta"  # posts/blah.meta
        self.folder = destination
        self.translations = translations
        self.default_lang = default_lang
        with codecs.open(self.metadata_path, "r", "utf8") as meta_file:
            meta_data = meta_file.readlines()
        while len(meta_data) < 5:
            meta_data.append("")
        default_title, default_pagename, self.date, self.tags, self.link = \
            [x.strip() for x in meta_data][:5]
        self.date = datetime.datetime.strptime(self.date, '%Y/%m/%d %H:%M')
        self.tags = [x.strip() for x in self.tags.split(',')]
        self.tags = filter(None, self.tags)
        self.compile_html = compile_html

        self.pagenames = {}
        self.titles = {}
        # Load internationalized titles
        for lang in translations:
            if lang == default_lang:
                self.titles[lang] = default_title
                self.pagenames[lang] = default_pagename
            else:
                metadata_path = self.metadata_path + "." + lang
                try:
                    with codecs.open(metadata_path, "r", "utf8") as meta_file:
                        meta_data = [x.strip() for x in meta_file.readlines()]
                        while len(meta_data) < 2:
                            meta_data.append("")
                        self.titles[lang] = meta_data[0] or default_title
                        self.pagenames[lang] = meta_data[1] or default_pagename
                except:
                    self.titles[lang] = default_title
                    self.pagenames[lang] = default_pagename

    def title(self, lang):
        """Return localized title."""
        return self.titles[lang]

    def deps(self, lang):
        """Return a list of dependencies to build this post's page."""
        deps = [self.base_path]
        if lang != self.default_lang:
            deps += [self.base_path + "." + lang]
        deps += self.fragment_deps(lang)
        return deps

    def fragment_deps(self, lang):
        """Return a list of dependencies to build this post's fragment."""
        deps = [self.source_path, self.metadata_path]
        if lang != self.default_lang:
            lang_deps = filter(os.path.exists, [x + "." + lang for x in deps])
            deps += lang_deps
        return deps

    def text(self, lang):
        """Read the post file for that language and return its contents"""
        file_name = self.base_path
        if lang != self.default_lang:
            file_name_lang = file_name + ".%s" % lang
            if os.path.exists(file_name_lang):
                file_name = file_name_lang
        with codecs.open(file_name, "r", "utf8") as post_file:
            data = post_file.read()
        return data

    def destination_path(self, lang, extension='.html'):
        path = os.path.join(self.translations[lang],
            self.folder, self.pagenames[lang] + extension)
        return path

    def permalink(self, lang=None, absolute=False, extension='.html'):
        if lang is None:
            lang = self.default_lang
        pieces = list(os.path.split(self.translations[lang]))
        pieces += list(os.path.split(self.folder))
        pieces += [self.pagenames[lang] + extension]
        pieces = filter(None, pieces)
        if absolute:
            pieces = [self.blog_url] + pieces
        else:
            pieces = [""] + pieces
        link = "/".join(pieces)
        return link


class Nikola(object):

    """Class that handles site generation.

    Takes a site config as argument on creation.
    """

    def __init__(self, **config):
        """Setup proper environment for running tasks."""

        self.global_data = {}
        self.posts_per_year = defaultdict(list)
        self.posts_per_tag = defaultdict(list)
        self.timeline = []
        self._scanned = False

        # This is the default config
        # TODO: fill it
        self.config = {
            'OUTPUT_FOLDER': 'output',
            'FILES_FOLDERS': ('files', ),
            'ADD_THIS_BUTTONS': True,
            'post_compilers': {
                "rest":     ['.txt', '.rst'],
                "markdown": ['.md', '.mdown', '.markdown']
            }

        }
        self.config.update(config)

        self.get_compile_html = utils.CompileHtmlGetter(
            self.config.pop('post_compilers'))

        self.GLOBAL_CONTEXT = self.config['GLOBAL_CONTEXT']
        self.THEMES = utils.get_theme_chain(self.config['THEME'])

        self.templates_module = utils.get_template_module(
            self.config['TEMPLATE_ENGINE'], self.THEMES)
        self.template_deps = self.templates_module.template_deps

        self.MESSAGES = utils.load_messages(self.THEMES,
            self.config['TRANSLATIONS'])
        self.GLOBAL_CONTEXT['messages'] = self.MESSAGES

        self.GLOBAL_CONTEXT['_link'] = self.link
        self.GLOBAL_CONTEXT['rel_link'] = self.rel_link
        self.GLOBAL_CONTEXT['exists'] = self.file_exists
        self.GLOBAL_CONTEXT['add_this_buttons'] = self.config[
            'ADD_THIS_BUTTONS']

        self.DEPS_CONTEXT = {}
        for k, v in self.GLOBAL_CONTEXT.items():
            if isinstance(v, (str, unicode, int, float, dict)):
                self.DEPS_CONTEXT[k] = v

    def render_template(self, template_name, output_name, context):
            self.templates_module.render_template(
                template_name, output_name, context, self.GLOBAL_CONTEXT)

    def path(self, kind, name, lang, is_link=False):
        """Build the path to a certain kind of page.

        kind is one of:

        * tag_index (name is ignored)
        * tag (and name is the tag name)
        * tag_rss (name is the tag name)
        * archive (and name is the year, or None for the main archive index)
        * index (name is the number in index-number)
        * rss (name is ignored)
        * gallery (name is the gallery name)

        The returned value is always a path relative to output, like
        "categories/whatever.html"

        If is_link is True, the path is absolute and uses "/" as separator
        (ex: "/archive/index.html").
        If is_link is False, the path is relative to output and uses the
        platform's separator.
        (ex: "archive\\index.html")
        """

        path = []

        if kind == "tag_index":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], 'index.html'])
        elif kind == "tag":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], name + ".html"])
        elif kind == "tag_rss":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['TAG_PATH'], name + ".xml"])
        elif kind == "index":
            if name > 0:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['INDEX_PATH'], 'index-%s.html' % name])
            else:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['INDEX_PATH'], 'index.html'])
        elif kind == "rss":
            path = filter(None, [self.config['TRANSLATIONS'][lang],
            self.config['RSS_PATH'], 'rss.xml'])
        elif kind == "archive":
            if name:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['ARCHIVE_PATH'], name, 'index.html'])
            else:
                path = filter(None, [self.config['TRANSLATIONS'][lang],
                self.config['ARCHIVE_PATH'], 'archive.html'])
        elif kind == "gallery":
            path = filter(None,
                [self.config['GALLERY_PATH'], name, 'index.html'])
        if is_link:
            return '/' + ('/'.join(path))
        else:
            return os.path.join(*path)

    def link(self, *args):
        return self.path(*args, is_link=True)

    def rel_link(self, src, dst):
        # Normalize
        src = urlparse.urljoin(self.config['BLOG_URL'], src)
        dst = urlparse.urljoin(src, dst)
        # Check that link can be made relative, otherwise return dest
        parsed_src = urlparse.urlsplit(src)
        parsed_dst = urlparse.urlsplit(dst)
        if parsed_src[:2] != parsed_dst[:2]:
            return dst
        # Now both paths are on the same site and absolute
        src_elems = parsed_src.path.split('/')[1:]
        dst_elems = parsed_dst.path.split('/')[1:]
        i = 0
        for (i, s), d in zip(enumerate(src_elems), dst_elems):
            if s != d:
                break
        else:
            i += 1
        # Now i is the longest common prefix
        return '/'.join(['..'] * (len(src_elems) - i - 1) + dst_elems[i:])

    def file_exists(self, path, not_empty=False):
        """Returns True if the file exists. If not_empty is True,
        it also has to be not empty."""
        exists = os.path.exists(path)
        if exists and not_empty:
            exists = os.stat(path).st_size > 0
        return exists

    def gen_tasks(self):

        yield self.task_serve(output_folder=self.config['OUTPUT_FOLDER'])
        yield self.task_install_theme()
        yield self.gen_task_new_post(self.config['post_pages'])
        yield self.gen_task_new_page(self.config['post_pages'])
        yield self.gen_task_copy_assets(themes=self.THEMES,
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_deploy(commands=self.config['DEPLOY_COMMANDS'])
        yield self.gen_task_sitemap(blog_url=self.config['BLOG_URL'],
            output_folder=self.config['OUTPUT_FOLDER']
        )
        yield self.gen_task_render_pages(
            translations=self.config['TRANSLATIONS'],
            post_pages=self.config['post_pages'])
        yield self.gen_task_render_sources(
            translations=self.config['TRANSLATIONS'],
            default_lang=self.config['DEFAULT_LANG'],
            output_folder=self.config['OUTPUT_FOLDER'],
            post_pages=self.config['post_pages'])
        yield self.gen_task_render_posts(
            translations=self.config['TRANSLATIONS'],
            default_lang=self.config['DEFAULT_LANG'],
            timeline=self.timeline
            )
        yield self.gen_task_render_indexes(
            translations=self.config['TRANSLATIONS'],
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_render_archive(
            translations=self.config['TRANSLATIONS'],
            messages=self.MESSAGES,
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_render_tags(
            translations=self.config['TRANSLATIONS'],
            messages=self.MESSAGES,
            blog_title=self.config['BLOG_TITLE'],
            blog_url=self.config['BLOG_URL'],
            blog_description=self.config['BLOG_DESCRIPTION'],
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_render_rss(
            translations=self.config['TRANSLATIONS'],
            blog_title=self.config['BLOG_TITLE'],
            blog_url=self.config['BLOG_URL'],
            blog_description=self.config['BLOG_DESCRIPTION'],
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_render_galleries(
            thumbnail_size=self.config['THUMBNAIL_SIZE'],
            default_lang=self.config['DEFAULT_LANG'],
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_redirect(
            redirections=self.config['REDIRECTIONS'],
            output_folder=self.config['OUTPUT_FOLDER'])
        yield self.gen_task_copy_files(
            output_folder=self.config['OUTPUT_FOLDER'],
            files_folders=self.config['FILES_FOLDERS'])
        yield {
            'name': 'all',
            'actions': None,
            'clean': True,
            'task_dep': [
                'render_archive',
                'render_galleries',
                'render_indexes',
                'render_pages',
                'render_posts',
                'render_rss',
                'render_sources',
                'render_tags',
                'copy_assets',
                'copy_files',
                'sitemap',
                'redirect',
                ],
            }

    def scan_posts(self):
        """Scan all the posts."""
        if not self._scanned:
            print "Scanning posts ",
            for wildcard, destination, _, use_in_feeds in self.config['post_pages']:
                print ".",
                for base_path in glob.glob(wildcard):
                    post = Post(base_path, destination, use_in_feeds,
                        self.config['TRANSLATIONS'], self.config['DEFAULT_LANG'],
                        self.config['BLOG_URL'],
                        self.get_compile_html(base_path))
                    self.global_data[post.post_name] = post
                    self.posts_per_year[str(post.date.year)].append(post.post_name)
                    for tag in post.tags:
                        self.posts_per_tag[tag].append(post.post_name)
            for name, post in self.global_data.items():
                self.timeline.append(post)
            self.timeline.sort(cmp=lambda a, b: cmp(a.date, b.date))
            self.timeline.reverse()
            self._scanned = True
            print "done!"

    def generic_page_renderer(self, lang, wildcard,
        template_name, destination):
        """Render post fragments to final HTML pages."""
        for post in glob.glob(wildcard):
            post_name = os.path.splitext(post)[0]
            context = {}
            post = self.global_data[post_name]
            deps = post.deps(lang) + self.template_deps(template_name)
            context['post'] = post
            context['lang'] = lang
            context['title'] = post.title(lang)
            context['permalink'] = post.permalink(lang)
            output_name = os.path.join(
                self.config['OUTPUT_FOLDER'],
                self.config['TRANSLATIONS'][lang],
                destination,
                post.pagenames[lang] + ".html")
            deps_dict = copy(context)
            deps_dict.pop('post')
            deps_dict['OUTPUT_FOLDER']=self.config['OUTPUT_FOLDER']
            deps_dict['TRANSLATIONS']=self.config['TRANSLATIONS']
            yield {
                'name': output_name.encode('utf-8'),
                'file_dep': deps,
                'targets': [output_name],
                'actions': [(self.render_template,
                    [template_name, output_name, context])],
                'clean': True,
                'uptodate': [config_changed(deps_dict)],
            }

    def gen_task_render_pages(self, **kw):
        """Build final pages from metadata and HTML fragments.

        Required keyword arguments:

        translations
        post_pages
        """
        self.scan_posts()
        for lang in kw["translations"]:
            for wildcard, destination, template_name, _ in kw["post_pages"]:
                for task in self.generic_page_renderer(
                        lang, wildcard, template_name, destination):
                    #task['uptodate'] = task.get('uptodate', []) +\
                        #[config_changed(kw)]
                    task['basename'] = 'render_pages'
                    yield task

    def gen_task_render_sources(self, **kw):
        """Publish the rst sources because why not?

        Required keyword arguments:

        translations
        default_lang
        post_pages
        output_folder
        """
        self.scan_posts()
        for lang in kw["translations"]:
            # TODO: timeline is global
            for post in self.timeline:
                output_name = os.path.join(kw['output_folder'],
                    post.destination_path(lang, '.txt'))
                source = post.source_path
                if lang != kw["default_lang"]:
                    source_lang = source + '.' + lang
                    if os.path.exists(source_lang):
                        source = source_lang
                yield {
                    'basename': 'render_sources',
                    'name': output_name.encode('utf8'),
                    'file_dep': [source],
                    'targets': [output_name],
                    'actions': [(utils.copy_file, (source, output_name))],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                    }

    def gen_task_render_posts(self, **kw):
        """Build HTML fragments from metadata and reSt.

        Required keyword arguments:

        translations
        default_lang
        timeline
        """
        self.scan_posts()
        for lang in kw["translations"]:
            # TODO: timeline is global, get rid of it
            deps_dict = copy(kw)
            deps_dict.pop('timeline')
            for post in kw['timeline']:
                source = post.source_path
                dest = post.base_path
                if lang != kw["default_lang"]:
                    dest += '.' + lang
                    source_lang = source + '.' + lang
                    if os.path.exists(source_lang):
                        source = source_lang
                yield {
                    'basename': 'render_posts',
                    'name': dest.encode('utf-8'),
                    'file_dep': post.fragment_deps(lang),
                    'targets': [dest],
                    'actions': [(post.compile_html, [source, dest])],
                    'clean': True,
                    'uptodate': [config_changed(deps_dict)],
                }

    def gen_task_render_indexes(self, **kw):
        """Render 10-post-per-page indexes.

        Required keyword arguments:

        translations
        output_folder
        """
        self.scan_posts()
        template_name = "index.tmpl"
        # TODO: timeline is global, get rid of it
        posts = [x for x in self.timeline if x.use_in_feeds]
        # Split in smaller lists
        lists = []
        while posts:
            lists.append(posts[:10])
            posts = posts[10:]
        num_pages = len(lists)
        if not lists:
            yield {
                'basename': 'render_indexes',
                'actions': [],
                }
        for lang in kw["translations"]:
            for i, post_list in enumerate(lists):
                context = {}
                if not i:
                    output_name = "index.html"
                else:
                    output_name = "index-%s.html" % i
                context["prevlink"] = None
                context["nextlink"] = None
                if i > 1:
                    context["prevlink"] = "index-%s.html" % (i - 1)
                if i == 1:
                    context["prevlink"] = "index.html"
                if i < num_pages - 1:
                    context["nextlink"] = "index-%s.html" % (i + 1)
                context["permalink"] = self.link("index", i, lang)
                output_name = os.path.join(
                    kw['output_folder'], self.path("index", i, lang))
                task = self.generic_post_list_renderer(
                    lang,
                    post_list,
                    output_name,
                    template_name,
                    context,
                )
                task['uptodate'] = task.get('updtodate', []) +\
                    [config_changed(kw)]
                task['basename'] = 'render_indexes'
                yield task

    def generic_post_list_renderer(self, lang, posts,
        output_name, template_name, extra_context={}):
        """Renders pages with lists of posts."""

        deps = self.template_deps(template_name)
        for post in posts:
            deps += post.deps(lang)
        context = {}
        context["posts"] = posts
        context["title"] = self.config['BLOG_TITLE']
        context["lang"] = lang
        context["prevlink"] = None
        context["nextlink"] = None
        context.update(extra_context)
        return {
            'name': output_name.encode('utf8'),
            'targets': [output_name],
            'file_dep': deps,
            'actions': [(self.render_template,
                [template_name, output_name, context])],
            'clean': True,
            'uptodate': [config_changed(context)]
        }

    def gen_task_render_archive(self, **kw):
        """Render the post archives.

        Required keyword arguments:

        translations
        messages
        output_folder
        """
        # TODO add next/prev links for years
        template_name = "list.tmpl"
        # TODO: posts_per_year is global, kill it
        for year, posts in self.posts_per_year.items():
            for lang in kw["translations"]:
                output_name = os.path.join(
                    kw['output_folder'], self.path("archive", year, lang))
                post_list = [self.global_data[post] for post in posts]
                post_list.sort(cmp=lambda a, b: cmp(a.date, b.date))
                post_list.reverse()
                context = {}
                context["lang"] = lang
                context["items"] = [("[%s] %s" %
                    (post.date, post.title(lang)), post.permalink(lang))
                    for post in post_list]
                context["permalink"] = self.link("archive", year, lang)
                context["title"] = kw["messages"][lang]["Posts for year %s"]\
                    % year
                task = self.generic_post_list_renderer(
                    lang,
                    post_list,
                    output_name,
                    template_name,
                    context,
                )
                task['uptodate'] = task.get('updtodate', []) +\
                    [config_changed(kw)]
                yield task

        # And global "all your years" page
        years = self.posts_per_year.keys()
        years.sort(reverse=True)
        template_name = "list.tmpl"
        for lang in kw["translations"]:
            context = {}
            output_name = os.path.join(
                kw['output_folder'], self.path("archive", None, lang))
            context["title"] = kw["messages"][lang]["Archive"]
            context["items"] = [(year, self.link("archive", year, lang))
                for year in years]
            context["permalink"] = self.link("archive", None, lang)
            task = self.generic_post_list_renderer(
                lang,
                [],
                output_name,
                template_name,
                context,
            )
            task['uptodate'] = task.get('updtodate', []) + [config_changed(kw)]
            task['basename'] = 'render_archive'
            yield task

    def gen_task_render_tags(self, **kw):
        """Render the tag pages.

        Required keyword arguments:

        translations
        messages
        blog_title
        blog_url
        blog_description
        output_folder
        """
        template_name = "list.tmpl"
        for tag, posts in self.posts_per_tag.items():
            for lang in kw["translations"]:
                # Render HTML
                output_name = os.path.join(kw['output_folder'],
                    self.path("tag", tag, lang))
                post_list = [self.global_data[post] for post in posts]
                post_list.sort(cmp=lambda a, b: cmp(a.date, b.date))
                post_list.reverse()
                context = {}
                context["lang"] = lang
                context["title"] = kw["messages"][lang][u"Posts about %s:"]\
                    % tag
                context["items"] = [("[%s] %s" % (post.date, post.title(lang)),
                    post.permalink(lang)) for post in post_list]
                context["permalink"] = self.link("tag", tag, lang)

                task = self.generic_post_list_renderer(
                    lang,
                    post_list,
                    output_name,
                    template_name,
                    context,
                )
                task['uptodate'] = task.get('updtodate', []) +\
                    [config_changed(kw)]
                task['basename'] = 'render_tags'
                yield task

                #Render RSS
                output_name = os.path.join(kw['output_folder'],
                    self.path("tag_rss", tag, lang))
                deps = []
                post_list = [self.global_data[post] for post in posts
                    if self.global_data[post].use_in_feeds]
                post_list.sort(cmp=lambda a, b: cmp(a.date, b.date))
                post_list.reverse()
                for post in post_list:
                    deps += post.deps(lang)
                yield {
                    'name': output_name.encode('utf8'),
                    'file_dep': deps,
                    'targets': [output_name],
                    'actions': [(utils.generic_rss_renderer,
                        (lang, "%s (%s)" % (kw["blog_title"], tag),
                        kw["blog_url"], kw["blog_description"],
                        post_list, output_name))],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                }

        # And global "all your tags" page
        tags = self.posts_per_tag.keys()
        tags.sort()
        template_name = "list.tmpl"
        for lang in kw["translations"]:
            output_name = os.path.join(
                kw['output_folder'], self.path('tag_index', None, lang))
            context = {}
            context["title"] = kw["messages"][lang][u"Tags"]
            context["items"] = [(tag, self.link("tag", tag, lang))
                for tag in tags]
            context["permalink"] = self.link("tag_index", None, lang)
            task = self.generic_post_list_renderer(
                lang,
                [],
                output_name,
                template_name,
                context,
            )
            task['uptodate'] = task.get('updtodate', []) + [config_changed(kw)]
            yield task

    def gen_task_render_rss(self, **kw):
        """Generate RSS feeds.

        Required keyword arguments:

        translations
        blog_title
        blog_url
        blog_description
        output_folder
        """

        self.scan_posts()
        # TODO: timeline is global, kill it
        for lang in kw["translations"]:
            output_name = os.path.join(kw['output_folder'],
                self.path("rss", None, lang))
            deps = []
            posts = [x for x in self.timeline if x.use_in_feeds][:10]
            for post in posts:
                deps += post.deps(lang)
            yield {
                'basename': 'render_rss',
                'name': output_name,
                'file_dep': deps,
                'targets': [output_name],
                'actions': [(utils.generic_rss_renderer,
                    (lang, kw["blog_title"], kw["blog_url"],
                    kw["blog_description"], posts, output_name))],
                'clean': True,
                'uptodate': [config_changed(kw)],
            }

    def gen_task_render_galleries(self, **kw):
        """Render image galleries.

        Required keyword arguments:

        thumbnail_size,
        default_lang,
        output_folder
        """
        template_name = "gallery.tmpl"

        gallery_list = glob.glob("galleries/*")
        # Fail quick if we don't have galleries, so we don't
        # require PIL
        Image = None
        if not gallery_list:
            yield {
                'basename': 'render_galleries',
                'actions': [],
                }
            return
        try:
            import Image as _Image
            Image = _Image
        except ImportError:
            try:
                from PIL import Image as _Image
                Image = _Image
            except ImportError:
                pass
        if Image:
            def create_thumb(src, dst):
                size = kw["thumbnail_size"], kw["thumbnail_size"]
                im = Image.open(src)
                im.thumbnail(size, Image.ANTIALIAS)
                im.save(dst)
        else:
            create_thumb = utils.copy_file

        # gallery_path is "gallery/name"
        for gallery_path in gallery_list:
            # gallery_name is "name"
            gallery_name = os.path.basename(gallery_path)
            # output_gallery is "output/GALLERY_PATH/name"
            output_gallery = os.path.dirname(os.path.join(kw["output_folder"],
                self.path("gallery", gallery_name, None)))
            if not os.path.isdir(output_gallery):
                yield {
                    'basename': 'render_galleries',
                    'name': output_gallery,
                    'actions': [(os.makedirs, (output_gallery,))],
                    'targets': [output_gallery],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                    }
            # image_list contains "gallery/name/image_name.jpg"
            image_list = glob.glob(gallery_path + "/*jpg") +\
                glob.glob(gallery_path + "/*JPG") +\
                glob.glob(gallery_path + "/*PNG") +\
                glob.glob(gallery_path + "/*png")
            image_list = [x for x in image_list if "thumbnail" not in x]
            image_name_list = [os.path.basename(x) for x in image_list]
            thumbs = []
            # Do thumbnails and copy originals
            for img, img_name in zip(image_list, image_name_list):
                # img is "galleries/name/image_name.jpg"
                # img_name is "image_name.jpg"
                # fname, ext are "image_name", ".jpg"
                fname, ext = os.path.splitext(img_name)
                # thumb_path is
                # "output/GALLERY_PATH/name/image_name.thumbnail.jpg"
                thumb_path = os.path.join(output_gallery,
                    fname + ".thumbnail" + ext)
                # thumb_path is "output/GALLERY_PATH/name/image_name.jpg"
                orig_dest_path = os.path.join(output_gallery, img_name)
                thumbs.append(os.path.basename(thumb_path))
                yield {
                    'basename': 'render_galleries',
                    'name': thumb_path,
                    'file_dep': [img],
                    'targets': [thumb_path, orig_dest_path],
                    'actions': [
                        (create_thumb, (img, thumb_path)),
                        (utils.copy_file, (img, orig_dest_path))
                    ],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                }
            output_name = os.path.join(output_gallery, "index.html")
            context = {}
            context["lang"] = kw["default_lang"]
            context["title"] = os.path.basename(gallery_path)
            thumb_name_list = [os.path.basename(x) for x in thumbs]
            context["images"] = zip(image_name_list, thumb_name_list)
            context["permalink"] = self.link("gallery", gallery_name, None)

            # Use galleries/name/index.txt to generate a blurb for
            # the gallery, if it exists
            index_path = os.path.join(gallery_path, "index.txt")
            index_dst_path = os.path.join(gallery_path, "index.html")
            if os.path.exists(index_path):
                compile_html = self.get_compile_html(index_path)
                yield {
                    'basename': 'render_galleries',
                    'name': index_dst_path.encode('utf-8'),
                    'file_dep': [index_path],
                    'targets': [index_dst_path],
                    'actions': [(compile_html,
                        [index_path, index_dst_path])],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                }

            def render_gallery(output_name, context, index_dst_path):
                if os.path.exists(index_dst_path):
                    with codecs.open(index_dst_path, "rb", "utf8") as fd:
                        context['text'] = fd.read()
                else:
                    context['text'] = ''
                self.render_template(template_name, output_name, context)

            yield {
                'basename': 'render_galleries',
                'name': gallery_path,
                'file_dep': self.template_deps(template_name) + image_list,
                'targets': [output_name],
                'actions': [(render_gallery,
                    (output_name, context, index_dst_path))],
                'clean': True,
                'uptodate': [config_changed(kw)],
            }

    @staticmethod
    def gen_task_redirect(**kw):
        """Generate redirections.

        Required keyword arguments:

        redirections
        output_folder
        """

        def create_redirect(src, dst):
            with codecs.open(src, "wb+", "utf8") as fd:
                fd.write(('<head>' +
                '<meta HTTP-EQUIV="REFRESH" content="0; url=%s">' +
                '</head>') % dst)

        if not kw['redirections']:
            # If there are no redirections, still needs to create a
            # dummy action so dependencies don't fail
            yield {
                'basename': 'redirect',
                'name': 'None',
                'uptodate': [True],
                'actions': [],
            }
        else:
            for src, dst in kw["redirections"]:
                src_path = os.path.join(kw["output_folder"], src)
                yield {
                    'basename': 'redirect',
                    'name': src_path,
                    'targets': [src_path],
                    'actions': [(create_redirect, (src_path, dst))],
                    'clean': True,
                    'uptodate': [config_changed(kw)],
                    }

    @staticmethod
    def gen_task_copy_files(**kw):
        """Copy static files into the output folder.

        required keyword arguments:

        output_folder
        files_folders
        """

        flag = False
        for src in kw['files_folders']:
            dst = kw['output_folder']

            for task in utils.copy_tree(src, dst):
                flag = True
                task['basename'] = 'copy_files'
                task['uptodate'] = task.get('uptodate', []) +\
                    [config_changed(kw)]
                yield task
        if not flag:
            yield {
                'basename': 'copy_files',
                'actions': (),
            }

    @staticmethod
    def gen_task_copy_assets(**kw):
        """Create tasks to copy the assets of the whole theme chain.

        If a file is present on two themes, use the version
        from the "youngest" theme.

        Required keyword arguments:

        themes
        output_folder

        """
        tasks = {}
        for theme_name in kw['themes']:
            src = os.path.join(utils.get_theme_path(theme_name), 'assets')
            dst = os.path.join(kw['output_folder'], 'assets')
            for task in utils.copy_tree(src, dst):
                if task['name'] in tasks:
                    continue
                tasks[task['name']] = task
                task['uptodate'] = task.get('uptodate', []) + \
                    [config_changed(kw)]
                task['basename'] = 'copy_assets'
                yield task

    @staticmethod
    def new_post(post_pages, is_post=True):
        # Guess where we should put this
        for path, _, _, use_in_rss in post_pages:
            if use_in_rss == is_post:
                break

        print "Creating New Post"
        print "-----------------\n"
        title = raw_input("Enter title: ").decode(sys.stdin.encoding)
        slug = utils.slugify(title)

        if not slug:
            try:
                from unidecode import unidecode
            except:
                print "Please run 'pip install unidecode' for unicode issues"
                exit()
            slug = utils.slugify(unidecode(title))

        data = u'\n'.join([
            title,
            slug,
            datetime.datetime.now().strftime('%Y/%m/%d %H:%M')
            ])
        output_path = os.path.dirname(path)
        meta_path = os.path.join(output_path, slug + ".meta")
        txt_path = os.path.join(output_path, slug + ".txt")

        if os.path.isfile(meta_path) or os.path.isfile(txt_path):
            print "The title already exists!"
            exit()

        with codecs.open(meta_path, "wb+", "utf8") as fd:
            fd.write(data)
        with codecs.open(txt_path, "wb+", "utf8") as fd:
            fd.write(u"Write your post here.")
        print "Your post's metadata is at: ", meta_path
        print "Your post's text is at: ", txt_path


    @classmethod
    def new_page(cls):
        cls.new_post(False)

    @classmethod
    def gen_task_new_post(cls, post_pages):
        """Create a new post (interactive)."""
        yield {
            "basename": "new_post",
            "actions": [PythonInteractiveAction(cls.new_post, (post_pages,))],
            }


    @classmethod
    def gen_task_new_page(cls, post_pages):
        """Create a new post (interactive)."""
        yield {
            "basename": "new_page",
            "actions": [PythonInteractiveAction(cls.new_post, (post_pages, False,))],
            }

    @staticmethod
    def gen_task_deploy(**kw):
        """Deploy site.

        Required keyword arguments:

        commands

        """
        yield {
            "basename": "deploy",
            "actions": kw['commands'],
            "verbosity": 2,
            }

    @staticmethod
    def gen_task_sitemap(**kw):
        """Generate Google sitemap.

        Required keyword arguments:

        blog_url
        output_folder
        """

        output_path = os.path.abspath(kw['output_folder'])
        sitemap_path = os.path.join(output_path, "sitemap.xml.gz")

        def sitemap():
            # Generate config
            config_data = """<?xml version="1.0" encoding="UTF-8"?>
    <site
    base_url="%s"
    store_into="%s"
    verbose="1" >
    <directory path="%s" url="%s" />
    <filter action="drop" type="wildcard" pattern="*~" />
    <filter action="drop" type="regexp" pattern="/\.[^/]*" />
    </site>""" % (
                kw["blog_url"],
                sitemap_path,
                output_path,
                kw["blog_url"],
            )
            config_file = tempfile.NamedTemporaryFile(delete=False)
            config_file.write(config_data)
            config_file.close()

            # Generate sitemap
            from tools import sitemap_gen as smap
            sitemap = smap.CreateSitemapFromFile(config_file.name, True)
            if not sitemap:
                smap.output.Log('Configuration file errors -- exiting.', 0)
            else:
                sitemap.Generate()
                smap.output.Log('Number of errors: %d' %
                    smap.output.num_errors, 1)
                smap.output.Log('Number of warnings: %d' %
                    smap.output.num_warns, 1)
            os.unlink(config_file.name)

        yield {
            "basename": "sitemap",
            "task_dep": [
                "render_archive",
                "render_indexes",
                "render_pages",
                "render_posts",
                "render_rss",
                "render_sources",
                "render_tags"],
            "targets": [sitemap_path],
            "actions": [(sitemap,)],
            "uptodate": [config_changed(kw)],
            "clean": True,
            }


    @staticmethod
    def task_serve(**kw):
        """
        Start test server. (Usage: doit serve [--address 127.0.0.1] [--port 8000])
        By default, the server runs on port 8000 on the IP address 127.0.0.1.
        """

        def serve(address, port):
            from BaseHTTPServer import HTTPServer
            from SimpleHTTPServer import SimpleHTTPRequestHandler as handler

            os.chdir(kw['output_folder'])

            httpd = HTTPServer((address, port), handler)
            sa = httpd.socket.getsockname()
            print "Serving HTTP on", sa[0], "port", sa[1], "..."
            httpd.serve_forever()

        yield {
            "basename": 'serve',
            "actions": [(serve,)],
            "verbosity": 2,
            "params": [{'short': 'a',
                        'name': 'address',
                        'long': 'address',
                        'type': str,
                        'default': '127.0.0.1',
                        'help': 'Bind address (default: 127.0.0.1)'},
                       {'short': 'p',
                        'name': 'port',
                        'long': 'port',
                        'type': int,
                        'default': 8000,
                        'help': 'Port number (default: 8000)'}],
            }

    @staticmethod
    def task_install_theme():
        """Install theme. (Usage: doit install_theme -n themename [-u URL] | [-l])."""

        def install_theme(name, url, listing):
            if name is None and not listing:
                print "This command needs either the -n or the -l option."
                return False
            data = urllib2.urlopen(url).read()
            data = json.loads(data)
            if listing:
                print "Themes:"
                print "-------"
                for theme in sorted(data.keys()):
                    print theme
                return True
            else:
                if name in data:
                    if os.path.isfile("themes"):
                        raise IOError, "the 'themes' isn't a directory!"
                    elif not os.path.isdir("themes"):
                        try:
                            os.makedirs("themes")
                        except:
                            raise OSError, "mkdir 'theme' error!"
                    print 'Downloading: %s' % data[name]
                    zip_file = StringIO()
                    zip_file.write(urllib2.urlopen(data[name]).read())
                    print 'Extracting: %s into themes' % name
                    utils.extract_all(zip_file)
                else:
                    print "Can't find theme %s" % name
                    return False

        yield {
            "basename": 'install_theme',
            "actions": [(install_theme,)],
            "verbosity": 2,
            "params": [
                {
                    'short': 'u',
                    'name': 'url',
                    'long': 'url',
                    'type': str,
                    'default': 'http://nikola.ralsina.com.ar/themes/index.json',
                    'help': 'URL for theme collection.'
                },
                {
                    'short': 'l',
                    'name': 'listing',
                    'long': 'list',
                    'type': bool,
                    'default': False,
                    'help': 'List available themes.'
                },
                {
                    'short': 'n',
                    'name': 'name',
                    'long': 'name',
                    'type': str,
                    'default': None,
                    'help': 'Name of theme to install.'
                }],
            }


def nikola_main():
    print "Starting doit..."
    os.system("doit -f %s" % __file__)
