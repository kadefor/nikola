########################################
# Jinja template handlers
########################################

import os

import jinja2

lookup = None


def get_template_lookup(directories):
    return jinja2.Environment(loader=jinja2.FileSystemLoader(
        directories,
        encoding='utf-8',
        ))


def render_template(template_name, output_name, context, global_context):
    template = lookup.get_template(template_name)
    context.update(global_context)
    try:
        os.makedirs(os.path.dirname(output_name))
    except:
        pass
    with open(output_name, 'w+') as output:
        output.write(template.render(**context).encode('utf8'))


def template_deps(template_name):
    # TODO Implement
    return []
