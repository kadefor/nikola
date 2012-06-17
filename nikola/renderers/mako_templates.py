########################################
# Mako template handlers
########################################

import os

from mako import util, lexer
from mako.lookup import TemplateLookup

lookup = None
cache = {}

def get_deps(filename):
    text = util.read_file(filename)
    lex = lexer.Lexer(text=text, filename=filename)
    lex.parse()

    deps = []
    for n in lex.template.nodes:
        if getattr(n, 'keyword', None) == "inherit":
            deps.append(n.attributes['file'])
        # TODO: include tags are not handled
    return deps

def get_template_lookup(directories):
    return TemplateLookup(
        directories=directories,
        module_directory='tmp',
        output_encoding='utf-8',
        )


def render_template(template_name, output_name, context, global_context):
    template = lookup.get_template(template_name)
    context.update(global_context)
    try:
        os.makedirs(os.path.dirname(output_name))
    except:
        pass
    with open(output_name, 'w+') as output:
        output.write(template.render(**context))


def template_deps(template_name):
    # We can cache here because depedencies should
    # not change between runs
    if cache.get(template_name, None) is None:
        template = lookup.get_template(template_name)
        dep_filenames = get_deps(template.filename)
        deps = [template.filename]
        for fname in dep_filenames:
            deps += template_deps(fname)
        cache[template_name] = deps
    return cache[template_name]
