"""Implementation of compile_html based on reStructuredText and docutils."""

__all__ = ['compile_html']

import codecs

########################################
# custom rst directives and renderer
########################################
from docutils.core import publish_parts
from docutils.parsers.rst import directives

from pygments_code_block_directive import code_block_directive
directives.register_directive('code-block', code_block_directive)

def compile_html(source, dest):
    with codecs.open(source, "r", "utf8") as in_file:
        data = in_file.read()

    parts = publish_parts(source=data,writer_name='html',settings_overrides={'initial_header_level': '2'})
    body = parts['body']

    with codecs.open(dest, "w+", "utf8") as out_file:
        out_file.write(body)
