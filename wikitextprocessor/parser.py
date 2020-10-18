# Simple WikiMedia markup (WikiText) syntax parser
#
# Copyright (c) 2020 Tatu Ylonen.  See file LICENSE and https://ylonen.org

import re
import enum
import html
from .parserfns import PARSER_FUNCTIONS
from .wikihtml import ALLOWED_HTML_TAGS
from .common import MAGIC_NOWIKI_CHAR, MAGIC_FIRST, MAGIC_LAST


# HTML tags that are also parsed in the preparse phase
PRE_PARSE_TAGS = ["noinclude", "includeonly", "onlyinclude"]

# Set of tags that can be parents of "flow" parents
HTML_FLOW_PARENTS = set(k for k, v in ALLOWED_HTML_TAGS.items()
                        if "flow" in v.get("content", [])
                        or "*" in v.get("content", []))

# Set of tags that can be parents of "phrasing" parents (includes those
# of flow parents since flow implies phrasing)
HTML_PHRASING_PARENTS = set(k for k, v in ALLOWED_HTML_TAGS.items()
                            if "phrasing" in v.get("content", []) or
                            "flow" in v.get("content", []) or
                            "*" in v.get("content", []))

# Mapping from HTML tag or "text" to permitted parent tags
HTML_PERMITTED_PARENTS = {
    k: ((HTML_FLOW_PARENTS
         if "flow" in v.get("parents", []) or "*" in v.get("parents", [])
         else set()) |
        (HTML_PHRASING_PARENTS
         if "phrasing" in v.get("parents", []) or "*" in v.get("parents", [])
         else set()) |
        set(v.get("parents", [])))
    for k, v in ALLOWED_HTML_TAGS.items()
}
HTML_PERMITTED_PARENTS["text"] = HTML_PHRASING_PARENTS


# MediaWiki magic words.  See https://www.mediawiki.org/wiki/Help:Magic_words
MAGIC_WORDS = set([
    "__NOTOC__",
    "__FORCETOC__",
    "__TOC__",
    "__NOEDITSECTION__",
    "__NEWSECTIONLINK__",
    "__NONEWSECTIONLINK__",
    "__NOGALLERY__",
    "__HIDDENCAT__",
    "__EXPECTUNUSEDCATEGORY__",
    "__NOCONTENTCONVERT__",
    "__NOCC__",
    "__NOTITLECONVERT__",
    "__NOTC__",
    "__START__",
    "__END__",
    "__INDEX__",
    "__NOINDEX__",
    "__STATICREDIRECT__",
    "__NOGLOBAL__",
    "__DISAMBIG__",
])


@enum.unique
class NodeKind(enum.Enum):
    """Node types in the parse tree."""

    # Root node of the tree.  This represents the parsed document.
    # Its arguments are [pagetitle].
    ROOT = enum.auto(),

    # Level2 subtitle.  Arguments are the title, children are what the section
    # contains.
    LEVEL2 = enum.auto(),

    # Level3 subtitle
    LEVEL3 = enum.auto(),

    # Level4 subtitle
    LEVEL4 = enum.auto(),

    # Level5 subtitle
    LEVEL5 = enum.auto(),

    # Level6 subtitle
    LEVEL6 = enum.auto(),

    # Content to be rendered in italic.  Content is in children.
    ITALIC = enum.auto(),

    # Content to be rendered in bold.  Content is in children.
    BOLD = enum.auto(),

    # Horizontal line.  No arguments or children.
    HLINE = enum.auto(),

    # A list.  Each list will be started with this node, also nested
    # lists.  Args contains the prefix used to open the list.
    # Children will contain LIST_ITEM nodes that belong to this list.
    # For definition lists the prefix ends in ";".
    LIST = enum.auto(),  # args = prefix for all items of this list

    # A list item.  Nested items will be in children.  Items on the same
    # level will be on the same level.  There is no explicit node for a list.
    # Args is directly the token for this item (not as a list).  Children
    # is what goes in this list item.  List items where the prefix ends in
    # ";" are definition list items.  For them, children contain the item
    # to be defined and node.attrs["def"] contains the definition, which has
    # the same format as children (i.e., a list of strings and WikiNode).
    LIST_ITEM = enum.auto(),  # args = token for this item

    # Preformatted text were markup is interpreted.  Content is in children.
    # Indicated in WikiText by starting lines with a space.
    PREFORMATTED = enum.auto(),  # Preformatted inline text

    # Preformatted text where markup is NOT interpreted.  Content is in
    # children. Indicated in WikiText by <pre>...</pre>.
    PRE = enum.auto(),  # Preformatted text where specials not interpreted

    # An internal Wikimedia link (marked with [[...]]).  The link arguments
    # are in args.  This tag is also used for media inclusion.  Links with
    # trailing word end immediately after the link have the trailing part
    # in link children.
    LINK = enum.auto(),

    # A template call (transclusion).  Template name is in first argument
    # and template arguments in subsequent args.  Children are not used.
    # In WikiText {{name|arg1|...}}.
    TEMPLATE = enum.auto(),

    # A template argument expansion.  Variable name is in first argument and
    # subsequent arguments in remaining arguments.  Children are not used.
    # In WikiText {{{name|...}}}
    TEMPLATE_ARG = enum.auto(),

    # A parser function invocation.  This is also used for built-in
    # variables such as {{PAGENAME}}.  Parser function name is in
    # first argument and subsequent arguments are its parameters.
    # Children are not used.  In WikiText {{name:arg1|arg2|...}}.
    PARSER_FN = enum.auto(),

    # An external URL.  The first argument is the URL.  The second optional
    # argument is the display text. Children are not used.
    URL = enum.auto(),

    # A table.  Content is in children.
    TABLE = enum.auto(),

    # A table caption (under TABLE).  Content is in children.
    TABLE_CAPTION = enum.auto(),

    # A table row (under TABLE).  Content is in children.
    TABLE_ROW = enum.auto(),

    # A table header cell (under TABLE_ROW).  Content is in children.
    # Rows where all cells are header cells are header rows.
    TABLE_HEADER_CELL = enum.auto(),

    # A table cell (under TABLE_ROW).  Content is in children.
    TABLE_CELL = enum.auto(),

    # A MediaWiki magic word.  The magic word is assigned directly to args
    # (not as a list).  Children are not used.
    MAGIC_WORD = enum.auto(),

    # HTML tag (open or close tag).  Pairs of open and close tags are
    # merged into a single node and the content between them is stored
    # in the node's children.  Args is the name of the tag directly
    # (i.e., not a list and always without a slash).  Attrs contains
    # attributes from the HTML start tag.  The special tags
    # <onlyinclude>, <noinclude>, and <includeonly> as well as WikiText-related
    # tags with HTML-like syntax also generate this tag (with the exception
    # of <pre> and <nowiki>, which are handled specially).
    HTML = enum.auto(),

    # XXX <ref ...> and <references />
    # XXX -{ ... }- syntax, see

    # XXX __NOTOC__


# Maps subtitle token to its kind
subtitle_to_kind = {
    "==": NodeKind.LEVEL2,
    "===": NodeKind.LEVEL3,
    "====": NodeKind.LEVEL4,
    "=====": NodeKind.LEVEL5,
    "======": NodeKind.LEVEL6,
}


# Maps subtitle node kind to its level.  Keys include all title/subtitle nodes
# (this is also used like a set of all subtitle kinds, including the root).
kind_to_level = { v: len(k) for k, v in subtitle_to_kind.items() }
kind_to_level[NodeKind.ROOT] = 1


# Node types that have arguments separated by the vertical bar (|)
HAVE_ARGS_KINDS = (
    NodeKind.LINK,
    NodeKind.TEMPLATE,
    NodeKind.TEMPLATE_ARG,
    NodeKind.PARSER_FN,
    NodeKind.URL,
)


# Node kinds that generate an error if they have not been properly closed.
MUST_CLOSE_KINDS = (
    NodeKind.ITALIC,
    NodeKind.BOLD,
    NodeKind.PRE,
    NodeKind.HTML,
    NodeKind.LINK,
    NodeKind.TEMPLATE,
    NodeKind.TEMPLATE_ARG,
    NodeKind.PARSER_FN,
    NodeKind.URL,
    NodeKind.TABLE,
)


class WikiNode(object):
    """Node in the parse tree for WikiMedia text."""

    __slots__ = (
        "kind",
        "args",
        "attrs",
        "children",
        "loc",
    )

    def __init__(self, kind, loc):
        assert isinstance(kind, NodeKind)
        assert isinstance(loc, int)
        self.kind = kind
        self.args = []  # List of lists
        self.attrs = {}
        self.children = []   # list of str and WikiNode
        self.loc = loc

    def __str__(self):
        return "<{}({}){} {}>".format(self.kind.name,
                                      self.args if isinstance(self.args, str)
                                      else ", ".join(map(repr, self.args)),
                                      self.attrs,
                                      ", ".join(map(repr, self.children)))

    def __repr__(self):
        return self.__str__()


def _parser_push(ctx, kind):
    """Pushes a new node of the specified kind onto the stack."""
    assert isinstance(kind, NodeKind)
    _parser_merge_str_children(ctx)
    node = WikiNode(kind, ctx.linenum)
    prev = ctx.stack[-1]
    prev.children.append(node)
    ctx.stack.append(node)
    ctx.suppress_special = False
    return node

def _parser_merge_str_children(ctx):
    """Merges multiple consecutive str children into one.  We merge them
    as a separate step, because this gives linear worst-case time, vs.
    quadratic worst case (albeit with lower constant factor) if we just
    added to the previously accumulated string in text_fn() instead."""
    node = ctx.stack[-1]
    lst = node.children
    lstlen = len(lst)
    for i in range(lstlen - 1, -1, -1):
        if not isinstance(lst[i], str):
            break
    else:
        # All children are strings
        s = "".join(lst)
        s = re.sub(MAGIC_NOWIKI_CHAR, "", s)
        node.children = []
        if s:
            node.children.append(s)
        return
    cnt = lstlen - i - 1
    if cnt == 0:
        return
    node.children = lst[:-cnt]
    s = "".join(lst[-cnt:])
    s = re.sub(MAGIC_NOWIKI_CHAR, "", s)
    if s:
        node.children.append(s)

def _parser_pop(ctx, warn_unclosed):
    """Pops a node from the stack.  If the node has arguments, this moves
    remaining children of the node into its arguments.  If ``warn_unclosed``
    is True, this warns about nodes that should be explicitly closed
    not having been closed.  Also performs certain other operations on
    the parse tree; this is a place for various kludges that manipulate
    the nodes when their parsing completes."""
    assert warn_unclosed in (True, False)
    _parser_merge_str_children(ctx)
    node = ctx.stack[-1]

    # Warn about unclosed syntaxes.
    if warn_unclosed and node.kind in MUST_CLOSE_KINDS:
        if node.kind == NodeKind.HTML:
            ctx.error("HTML tag <{}> not properly closed, started on "
                       "line {}".format(node.args, node.loc))
        elif node.kind == NodeKind.PARSER_FN:
            ctx.error("parser function {!r} not properly closed, started on "
                      "line {}".format(node.args[0], node.loc))
        else:
            ctx.error("{} not properly closed, started on line {}"
                       "".format(node.kind.name, node.loc))

    # When popping BOLD and ITALIC nodes, if the node has no children,
    # just remove the node from it's parent's children.  We may otherwise
    # generate spurious empty BOLD and ITALIC nodes when closing them
    # out-of-order (which happens always with '''''bolditalic''''').
    if node.kind in (NodeKind.BOLD, NodeKind.ITALIC) and not node.children:
        ctx.stack.pop()
        assert ctx.stack[-1].children[-1].kind == node.kind
        ctx.stack[-1].children.pop()
        return

    # If the node has arguments, move remamining children to be the last
    # argument
    if node.kind in HAVE_ARGS_KINDS:
        node.args.append(node.children)
        node.children = []

    # When popping a TEMPLATE, check if its name is a constant that
    # is a known parser function (including predefined variable).
    # If so, turn this node into a PARSER_FN node.
    if (node.kind == NodeKind.TEMPLATE and node.args and
        len(node.args[0]) == 1 and isinstance(node.args[0][0], str) and
        node.args[0][0] in PARSER_FUNCTIONS):
        # Change node type to PARSER_FN.  Otherwise it has identical
        # structure to a TEMPLATE.
        node.kind = NodeKind.PARSER_FN

    # When popping description list nodes that have a definition,
    # shuffle attrs["head"] and children to have head in children and
    # definition in attrs["def"]
    if (node.kind == NodeKind.LIST_ITEM and node.args.endswith(";") and
        "head" in node.attrs):
        head = node.attrs["head"]
        del node.attrs["head"]
        node.attrs["def"] = node.children
        node.children = head

    # Remove the topmost node from the stack.  It should be on its parent's
    # chilren list.
    ctx.stack.pop()

def _parser_have(ctx, kind):
    """Returns True if any node on the stack is of the given kind."""
    assert isinstance(kind, NodeKind)
    for node in ctx.stack:
        if node.kind == kind:
            return True
    return False


def text_fn(ctx, token):
    """Inserts the token as raw text into the parse tree."""
    node = ctx.stack[-1]

    # Whitespaces inside an external link divide its first argument from its
    # second argument.  All remaining words go into the second argument.
    if token.isspace() and node.kind == NodeKind.URL and not node.args:
        _parser_merge_str_children(ctx)
        node.args.append(node.children)
        node.children = []
        return

    # Some nodes are automatically popped on newline/text
    if ctx.beginning_of_line:
        while True:
            node = ctx.stack[-1]
            if node.kind == NodeKind.LIST_ITEM:
                if token.startswith(" ") or token[0].startswith("\t"):
                    node.children.append(token)
                    return
                _parser_merge_str_children(ctx)
                if (node.children and isinstance(node.children[-1], str) and
                    not node.children[-1].isspace() and
                    node.children[-1].endswith("\n")):
                    _parser_pop(ctx, False)
                    continue
            elif node.kind == NodeKind.LIST:
                _parser_pop(ctx, False)
                continue
            elif node.kind == NodeKind.PREFORMATTED:
                _parser_merge_str_children(ctx)
                if (node.children and isinstance(node.children[-1], str) and
                    node.children[-1].endswith("\n") and
                    not token.startswith(" ") and not token.isspace()):
                    _parser_pop(ctx, False)
                    continue
            break

        # Spaces at the beginning of a line indicate preformatted text
        if token.startswith(" ") or token.startswith("\t"):
            if node.kind != NodeKind.PREFORMATTED:
                node = _parser_push(ctx, NodeKind.PREFORMATTED)

    # If the previous child was a link that doesn't yet have children,
    # and the text to be added starts with valid word characters, assume
    # they are link trail and add them as a child of the link.
    if (node.children and isinstance(node.children[-1], WikiNode) and
        node.children[-1].kind == NodeKind.LINK and
        not node.children[-1].children and
        not ctx.suppress_special):
        m = re.match(r"(?s)(\w+)(.*)", token)
        if m:
            node.children[-1].children.append(m.group(1))
            token = m.group(2)
            if not token:
                return

    # Add a text child
    node.children.append(token)


def hline_fn(ctx, token):
    """Processes a horizontal line token."""
    # Pop nodes from the stack until we reach a LEVEL2 subtitle or a
    # table element.  We also won't pop HTML nodes as they might appear
    # in template definitions.
    while True:
        node = ctx.stack[-1]
        if node.kind in (NodeKind.ROOT, NodeKind.LEVEL2,
                         NodeKind.TABLE, NodeKind.TABLE_CAPTION,
                         NodeKind.TABLE_ROW, NodeKind.TABLE_HEADER_CELL,
                         NodeKind.TABLE_CELL, NodeKind.HTML):
            break
        _parser_pop(ctx, True)

    _parser_push(ctx, NodeKind.HLINE)
    _parser_pop(ctx, True)


def subtitle_start_fn(ctx, token):
    """Processes a subtitle start token.  The token has < prepended to it."""
    assert isinstance(token, str)
    if ctx.pre_parse:
        return text_fn(ctx, token)

    kind = subtitle_to_kind[token[1:]]
    level = kind_to_level[kind]

    # Keep popping subtitles and other formats until the next subtitle
    # is of a higher level - but only if there are remaining subtitles.
    # Subtitles sometimes occur inside <noinclude> and similar tags, and we
    # don't want to force closing those.
    while any(x.kind in kind_to_level for x in ctx.stack):
        node = ctx.stack[-1]
        if kind_to_level.get(node.kind, 99) < level:
            break
        if node.kind == NodeKind.HTML:
            break
        _parser_pop(ctx, True)

    # Push the subtitle node.  Subtitle start nodes are guaranteed to have
    # a close node, though the close node could have an incorrect level.
    _parser_push(ctx, kind)


def subtitle_end_fn(ctx, token):
    """Processes a subtitle end token.  The token has > prepended to it."""
    assert isinstance(token, str)
    if ctx.pre_parse:
        return text_fn(ctx, token)

    kind = subtitle_to_kind[token[1:]]

    # Keep popping formats until we get to the subtitle node
    while True:
        node = ctx.stack[-1]
        if node.kind in kind_to_level:
            break
        _parser_pop(ctx, True)

    # Move children of the subtitle node to be its first argument.
    node = ctx.stack[-1]
    if node.kind != kind:
        ctx.error("subtitle start and end markers level mismatch")
    _parser_merge_str_children(ctx)
    node.args.append(node.children)
    node.children = []


def italic_fn(ctx, token):
    """Processes an italic start/end token ('')."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    if not _parser_have(ctx, NodeKind.ITALIC):
        # Push new formatting node
        _parser_push(ctx, NodeKind.ITALIC)
        return

    # Pop the italic.  If there is an intervening BOLD, push it afterwards
    # to allow closing them in either order.
    push_bold = False
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.ITALIC:
            _parser_pop(ctx, False)
            break
        if node.kind == NodeKind.BOLD:
            push_bold = True
        _parser_pop(ctx, False)
    if push_bold:
        _parser_push(ctx, NodeKind.BOLD)


def bold_fn(ctx, token):
    """Processes a bold start/end token (''')."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    if not _parser_have(ctx, NodeKind.BOLD):
        # Push new formatting node
        _parser_push(ctx, NodeKind.BOLD)
        return

    # Pop the bold.  If there is an intervening ITALIC, push it afterwards
    # to allow closing them in either order.
    push_italic = False
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.BOLD:
            _parser_pop(ctx, False)
            break
        if node.kind == NodeKind.ITALIC:
            push_italic = True
        _parser_pop(ctx, False)
    if push_italic:
        _parser_push(ctx, NodeKind.ITALIC)


def ilink_start_fn(ctx, token):
    """Processes an internal link start token "[["."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    _parser_push(ctx, NodeKind.LINK)


def elink_start_fn(ctx, token):
    """Processes an external link start token "["."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    _parser_push(ctx, NodeKind.URL)


def elink_end_fn(ctx, token):
    """Processes an external link end token "]"."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    if not _parser_have(ctx, NodeKind.URL):
        return text_fn(ctx, token)
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.URL:
            _parser_pop(ctx, False)
            break
        _parser_pop(ctx, True)


def url_fn(ctx, token):
    """Processes an URL written as URL in the text (an external link is
    automatically generated)."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    node = ctx.stack[-1]
    if node.kind == NodeKind.URL:
        return text_fn(ctx, token)
    node = _parser_push(ctx, NodeKind.URL)
    text_fn(ctx, token)
    _parser_pop(ctx, False)


def magic_fn(ctx, token):
    """Handler for a magic character used to encode templates, template
    arguments, and parser function calls."""
    idx = ord(token) - MAGIC_FIRST
    kind, args, nowiki = ctx.cookies[idx]
    ctx.beginning_of_line = False
    if kind == "T":
        if nowiki:
            process_text(ctx, "&lbrace;&lbrace;" +
                         "&vert;".join(args) +
                         "&rbrace;&rbrace;")
            return
        # Template tranclusion or parser function call
        _parser_push(ctx, NodeKind.TEMPLATE)

        # Process arguments
        process_text(ctx, args[0])
        for arg in args[1:]:
            vbar_fn(ctx, "|")
            process_text(ctx, arg)

        while True:
            node = ctx.stack[-1]
            if node.kind == NodeKind.ROOT:
                break
            if node.kind in (NodeKind.TEMPLATE, NodeKind.PARSER_FN):
                _parser_pop(ctx, False)
                break
            _parser_pop(ctx, True)

    elif kind == "A":
        if nowiki:
            process_text(ctx, "&lbrace;&lbrace;&lbrace;" +
                         "&vert;".join(args) +
                         "&rbrace;&rbrace;&rbrace;")
            return
        # Template argument reference
        _parser_push(ctx, NodeKind.TEMPLATE_ARG)

        # Process arguments
        process_text(ctx, args[0])
        for arg in args[1:]:
            vbar_fn(ctx, "|")
            process_text(ctx, arg)

        while True:
            node = ctx.stack[-1]
            if node.kind == NodeKind.ROOT:
                break
            if node.kind == NodeKind.TEMPLATE_ARG:
                _parser_pop(ctx, False)
                break
            _parser_pop(ctx, True)

    elif kind == "L":
        assert len(args) == 1
        if nowiki:
            process_text(ctx, "&lsqb;&lsqb;" + args[0] + "&rsqb;&rsqb;")
            return
        # Link to another page
        _parser_push(ctx, NodeKind.LINK)

        # Process the only argument
        assert len(args) == 1
        process_text(ctx, args[0])

        while True:
            node = ctx.stack[-1]
            if node.kind == NodeKind.ROOT:
                break
            if node.kind == NodeKind.LINK:
                _parser_pop(ctx, False)
                break
            _parser_pop(ctx, True)
    else:
        self.error("magic_fn: unsupported cookie kind {!r}"
                   .format(kind))


def colon_fn(ctx, token):
    """Handler for a special colon ":" within a template call.  This indicates
    that it is actually a parser function call.  This is called from list_fn()
    when it detects that it is inside a template node."""
    node = ctx.stack[-1]

    # Unless we are in the first argument of a template, treat a colon that is
    # not at the beginning of a
    if node.kind != NodeKind.TEMPLATE or node.args:
        return text_fn(ctx, token)

    # Merge string children.  This is needed for both the following text and
    # for args.
    _parser_merge_str_children(ctx)

    # Check if the template argument is a parser function name.
    if (len(node.children) != 1 or not isinstance(node.children[0], str) or
        node.children[0] not in PARSER_FUNCTIONS):
        return text_fn(ctx, token)

    # Colon in the first argument of {{name:...}} turns it into a parser
    # function call.
    _parser_merge_str_children(ctx)
    node.kind = NodeKind.PARSER_FN
    node.args.append(node.children)
    node.children = []


def table_start_fn(ctx, token):
    """Handler for table start token "{|"."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    _parser_push(ctx, NodeKind.TABLE)


def table_check_attrs(ctx):
    """Checks if the table has attributes, and if so, parses them."""
    node = ctx.stack[-1]
    if node.kind != NodeKind.TABLE:
        return
    _parser_merge_str_children(ctx)
    if len(node.children) != 1 or not isinstance(node.children[0], str):
        return
    attrs = node.children.pop()
    parse_attrs(node, attrs)


def table_row_check_attrs(ctx):
    """Checks if the table row has attributes, and if so, parses them."""
    node = ctx.stack[-1]
    if node.kind != NodeKind.TABLE_ROW:
        return
    _parser_merge_str_children(ctx)
    if len(node.children) != 1 or not isinstance(node.children[0], str):
        return
    attrs = node.children.pop()
    parse_attrs(node, attrs)


def table_caption_fn(ctx, token):
    """Handler for table caption token "|+"."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    table_check_attrs(ctx)
    if not _parser_have(ctx, NodeKind.TABLE):
        return text_fn(ctx, token)
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.TABLE:
            break
        _parser_pop(ctx, True)
    _parser_push(ctx, NodeKind.TABLE_CAPTION)


def table_hdr_cell_fn(ctx, token):
    """Handler function for table header row cell separator ! or !!."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    table_row_check_attrs(ctx)
    table_check_attrs(ctx)
    if not _parser_have(ctx, NodeKind.TABLE):
        return text_fn(ctx, token)
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.TABLE_ROW:
            _parser_push(ctx, NodeKind.TABLE_HEADER_CELL)
            return
        if node.kind == NodeKind.TABLE:
            _parser_push(ctx, NodeKind.TABLE_ROW)
            _parser_push(ctx, NodeKind.TABLE_HEADER_CELL)
            return
        if node.kind == NodeKind.TABLE_CAPTION:
            if ctx.beginning_of_line:
                _parser_pop(ctx, False)
                _parser_push(ctx, NodeKind.TABLE_ROW)
                _parser_push(ctx, NodeKind.TABLE_HEADER_CELL)
            else:
                text_fn(ctx, token)
            return
        if node.kind == NodeKind.TABLE_CELL:
            return text_fn(ctx, token)
        _parser_pop(ctx, True)


def table_row_fn(ctx, token):
    """Handler function for table row separator "|-"."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    table_check_attrs(ctx)
    if not _parser_have(ctx, NodeKind.TABLE):
        return text_fn(ctx, token)
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.TABLE:
            break
        _parser_pop(ctx, True)
    _parser_push(ctx, NodeKind.TABLE_ROW)


def table_cell_fn(ctx, token):
    """Handler function for table row cell separator | or ||."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    table_row_check_attrs(ctx)
    table_check_attrs(ctx)

    if not _parser_have(ctx, NodeKind.TABLE):
        return text_fn(ctx, token)

    if token == "|" and not ctx.beginning_of_line:
        # This might separate attributes for captions, header cells, and
        # data cells
        _parser_merge_str_children(ctx)
        node = ctx.stack[-1]
        if (not node.attrs and len(node.children) == 1 and
            isinstance(node.children[0], str)):
            if node.kind in (NodeKind.TABLE_CAPTION,
                             NodeKind.TABLE_HEADER_CELL,
                             NodeKind.TABLE_CELL):
                attrs = node.children.pop()
                parse_attrs(node, attrs)
                return
        return text_fn(ctx, token)

    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.TABLE_ROW:
            break
        if node.kind == NodeKind.TABLE:
            _parser_push(ctx, NodeKind.TABLE_ROW)
            break
        if node.kind == NodeKind.TABLE_CAPTION:
            return text_fn(ctx, token)
        _parser_pop(ctx, True)
    _parser_push(ctx, NodeKind.TABLE_CELL)


def vbar_fn(ctx, token):
    """Handler function for vertical bar |.  The interpretation of
    the vertical bar depends on context; it can separate arguments to
    templates, template argument references, links, etc, and it can
    also separate table row cells."""
    node = ctx.stack[-1]
    if node.kind in HAVE_ARGS_KINDS:
        _parser_merge_str_children(ctx)
        node.args.append(node.children)
        node.children = []
        return

    table_cell_fn(ctx, token)


def double_vbar_fn(ctx, token):
    """Handle function for double vertical bar ||.  This is used as a column
    separator in tables.  If it occurs in other contexts, it should be
    interpreted as two vertical bars."""
    node = ctx.stack[-1]
    if node.kind in HAVE_ARGS_KINDS:
        vbar_fn(ctx, "|")
        vbar_fn(ctx, "|")
        return

    table_cell_fn(ctx, token)


def table_end_fn(ctx, token):
    """Handler function for end of a table token "|}"."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    if not _parser_have(ctx, NodeKind.TABLE):
        return text_fn(ctx, token)
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.TABLE:
            _parser_pop(ctx, False)
            break
        _parser_pop(ctx, True)


def list_fn(ctx, token):
    """Handles various tokens that start unordered or ordered list items,
    description list items, or indented lines.  This also recognizes the
    colon used to separate parser function name from its first argument."""
    if ctx.pre_parse:
        return text_fn(ctx, token)

    node = ctx.stack[-1]

    # A colon inside a template means it is a parser function call.  We use
    # colon_fn() to handle that kind of colon.
    if token == ":" and node.kind == NodeKind.TEMPLATE:
        colon_fn(ctx, token)
        return

    # Colons can occur inside links and don't mean a list item
    if node.kind in (NodeKind.LINK, NodeKind.URL):
        return text_fn(ctx, token)

    # List items must start a new line; otherwise treat as text.  This is
    # particularly the case for colon, which is recognized as a token also
    # in the middle of a line.  Some of these cases were handled above; some
    # are handled here.
    if not ctx.beginning_of_line:
        node = ctx.stack[-1]
        if (token == ":" and node.kind == NodeKind.LIST_ITEM and
            node.args.endswith(";") and "head" not in node.attrs):
            # Got definition for a head in a definition list on the same line
            # Shuffle attrs["head"] and children (they will be unshuffled
            # in _parser_pop()) and do not change the stack otherwise
            _parser_merge_str_children(ctx)
            node.attrs["head"] = node.children
            node.children = []
            return
        # Otherwise treat colons that do not start a line as normal text
        return text_fn(ctx, token)

    # Pop any lower-level list items
    while True:
        node = ctx.stack[-1]

        # Check for a definition in a definition list
        if (node.kind == NodeKind.LIST_ITEM and node.args.endswith(";") and
            token.endswith(":") and token[:-1] == node.args[:-1] and
            "head" not in node.attrs):
            # Got definition for a definition list item, on a separate line.
            # Shuffle attrs["head"] and children (they will be unshuffled in
            # _parser_pop()) and do not change the stack otherwise
            _parser_merge_str_children(ctx)
            node.attrs["head"] = node.children
            node.children = []
            return

        # Check for continuing an earlier list item, possibly after an
        # intervening sublist
        if (node.kind == NodeKind.LIST_ITEM and token.endswith(":") and
            node.args == token[:-1]):
            # Suffixing a list item prefix with a colon can be used to continue
            # the same item after an intervening sublist.  In this case we
            # just return with the continued list item at the top of the stack.
            return

        # Check for another list item on the same level (adding a new
        # list item to an earlier list)
        if node.kind == NodeKind.LIST_ITEM and node.args == token:
            _parser_pop(ctx, False)
            break

        # Check for adding an item to the same list.  If the list has a
        # different prefix, we will close it and either add to a parent list
        # or start a new list.  Note that definition list definitions were
        # already handled above so we won't be seeing them here.
        if node.kind == NodeKind.LIST_ITEM and len(node.args) < len(token):
            for i in range(len(node.args)):
                if token[i] not in (":", node.args[i]):
                    break  # Tokens do not match
            else:
                # Tokens match (with non-last : matching * or #)
                # Create a sublist
                break

        # Stop popping if we are at a header.  Headers cannot be used inside
        # list items.  In this case we always start a new list.
        if node.kind in kind_to_level:
            break  # Always break before section header

        # There are various kinds of nodes that can contain lists.  We won't
        # pop them.
        if node.kind in (NodeKind.HTML, NodeKind.TEMPLATE,
                         NodeKind.TEMPLATE_ARG, NodeKind.PARSER_FN,
                         NodeKind.TABLE,
                         NodeKind.TABLE_HEADER_CELL,
                         NodeKind.TABLE_ROW,
                         NodeKind.TABLE_CELL):
            break

        # Otherwise pop the current node, possibly causing an error message.
        # For example, italics or bold must be contained in a single list item.
        _parser_pop(ctx, True)

    # If not already in a list, create a new list.
    node = ctx.stack[-1]
    if node.kind != NodeKind.LIST:
        node = _parser_push(ctx, NodeKind.LIST)
        node.args = token

    # Add a new list item to the list.
    node = _parser_push(ctx, NodeKind.LIST_ITEM)
    node.args = token


def parse_attrs(node, attrs):
    """Parses HTML tag attributes from ``attrs`` and adds them to
    ``node.attrs``."""
    assert isinstance(node, WikiNode)
    assert isinstance(attrs, str)

    # Extract attributes from the tag into the node.attrs dictionary
    for m in re.finditer(r"""(?si)\b([^"'>/=\0-\037\s]+)"""
                         r"""(=("[^"]*"|'[^']*'|[^"'<>`\s]*))?\s*""",
                         attrs):
        name = m.group(1)
        value = m.group(3) or ""
        if value.startswith("'") or value.startswith('"'):
            value = value[1:-1]
        node.attrs[name] = value


def tag_fn(ctx, token):
    """Handler function for tokens that look like HTML tags and their end
    tags.  This includes various built-in tags that aren't actually
    HTML.  Some WikiText tags that resemble HTML are described as HTML
    nodes, even though they are not really HTML."""

    # Note: <nowiki> and HTML comments have already been handled in
    # preprocessing

    # Try to parse it as a start tag
    m = re.match(r"""<\s*([-a-zA-Z0-9]+)\s*((\b[-a-z0-9]+(=("[^"]*"|"""
                 r"""'[^']*'|[^ \t\n"'`=<>/]*))?\s*)*)(/?)\s*>""", token)
    if m:
        # This is a start tag
        name = m.group(1)
        attrs = m.group(2)
        also_end = m.group(6) == "/"
        name = name.lower()

        # If preparsing, only handle template control tags like <noinclude>
        if ctx.pre_parse and name not in PRE_PARSE_TAGS:
            return text_fn(ctx, token)

        # Check for unmatched <nowiki> start tag.  <nowiki> should be handled
        # in preprocessing, but an unmatched start tag may be missed.
        if name == "nowiki":
            if also_end:
                # XXX mark current link/template/templatearg/parserfn as
                # nowiki, cause it to be expanded by escaped plaintext
                return
            ctx.error("unmatched <nowiki>")
            return text_fn(ctx, token)

        # Handle <pre> start tag
        if name == "pre":
            node = _parser_push(ctx, NodeKind.PRE)
            parse_attrs(node, attrs)
            if also_end:
                _parser_pop(ctx, False)
            return

        # Give a warning on unsupported HTML tags.  WikiText limits the set of
        # tags that are allowed.
        if name not in ALLOWED_HTML_TAGS:
            # Wiktionary seems to use markings like <3> in some
            # languages.  Treat them as text.  This method of handling
            # them may need to be reconsidered in the future if
            # problems arise.
            if not name.isdigit():
                ctx.warning("html tag <{}{}> not allowed in WikiText"
                            "".format(name, "/" if also_end else ""))
            return text_fn(ctx, token)

        # Automatically close parent HTML tags that should be ended by this tag
        # until we have a parent that is not a HTML tag or that is an allowed
        # parent for this node
        permitted_parents = HTML_PERMITTED_PARENTS.get(name, set())
        while True:
            node = ctx.stack[-1]
            if node.kind != NodeKind.HTML:
                break
            if node.args in permitted_parents:
                break
            close_next = ALLOWED_HTML_TAGS.get(node.args, {}).get(
                "close-next", [])
            # Warn about unclosed tag unless it is one we close automatically
            _parser_pop(ctx, name not in close_next)

        # Handle other start tag.  We push HTML tags as HTML nodes.
        node = _parser_push(ctx, NodeKind.HTML)
        node.args = name
        parse_attrs(node, attrs)

        # If the tag contains a trailing slash or it is an empty tag,
        # close it immediately.
        no_end_tag = ALLOWED_HTML_TAGS.get(name, {}).get("no-end-tag")
        if no_end_tag or also_end:
            _parser_pop(ctx, False)
        return

    # Since it was not a start tag, it should be an end tag
    m = re.match(r"<\s*/\s*([-a-zA-Z0-9]+)\s*>", token)
    assert m  # If fails, then mismatch between regexp here and tokenization
    name = m.group(1)
    name = name.lower()

    # If preparsing, only handle template control tags like <noinclude>
    if ctx.pre_parse and name not in PRE_PARSE_TAGS:
        return text_fn(ctx, token)

    if name == "pre":
        # Handle </pre> end tag
        node = ctx.stack[-1]
        if node.kind != NodeKind.PRE:
            ctx.error("unexpected </pre>")
            return text_fn(ctx, token)
        _parser_pop(ctx, False)
        return

    # Give a warning on unsupported HTML tags.  WikiText limits the set of
    # tags that are allowed.
    if name not in ALLOWED_HTML_TAGS:
        ctx.warning("html tag </{}> not allowed in WikiText"
                    "".format(name))
        return text_fn(ctx, token)

    # See if we can find the opening tag from the stack
    for i in range(0, len(ctx.stack)):
        node = ctx.stack[i]
        if node.kind == NodeKind.HTML and node.args == name:
            break
    else:
        # No corresponding start tag found
        if name in ("br", "hl", "wbr"):
            # This is incorrect but occurs; synthesize empty tag
            node = _parser_push(ctx, NodeKind.HTML)
            node.args = name
            _parser_pop(ctx, False)
            return
        ctx.error("no corresponding start tag found for {}".format(token))
        return

    # Close nodes until we close the corresponding start tag
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.HTML and node.args == name:
            # Found the corresponding start tag.  Close this node and
            # then stop.
            _parser_pop(ctx, False)
            break
        if node.kind == NodeKind.HTML:
            # If close-next is set, then end tag is optional and can be closed
            # implicitly by closing the parent tag
            close_next = ALLOWED_HTML_TAGS.get(node.args, {}).get(
                "close-next", None)
            if close_next:
                _parser_pop(ctx, False)
                continue
        _parser_pop(ctx, True)


def magicword_fn(ctx, token):
    """Handles a magic word, such as "__NOTOC__"."""
    node = _parser_push(ctx, NodeKind.MAGIC_WORD)
    node.args = token
    _parser_pop(ctx, False)


# Regular expression for matching a token in WikiMedia text.  This is used for
# tokenizing the input.
token_re = re.compile(r"(?m)^(={2,6})\s*(([^=]|=[^=])+?)\s*(={2,6})\s*$|"
                      r"'''|"
                      r"''|"
                      r"[ \t]+\n*|"
                      r"\n+|"
                      r"\[|"
                      r"\]|"
                      r"\|\}|"
                      r"\{\||"
                      r"\|\+|"
                      r"\|-|"
                      r"!!|"
                      r"^!|"
                      r"\|\||"
                      r"\||"
                      r"^----+|"
                      r"^[*:;#]+|"
                      r":|"   # sometimes special when not beginning of line
                      r"""<\s*[-a-zA-Z0-9]+\s*(\b[-a-z0-9]+(=("[^"]*"|"""
                        r"""'[^']*'|[^ \t\n"'`=<>]*))?\s*)*(/\s*)?>|"""
                      r"<\s*/\s*[-a-zA-Z0-9]+\s*>|"
                      r"https?://[a-zA-Z0-9.]+(/[^] ]*[^].])?|"
                      r"(" +
                      r"|".join(r"\b{}\b".format(x) for x in MAGIC_WORDS) +
                      r")|" +
                      r"[{:c}-{:c}]".format(MAGIC_FIRST, MAGIC_LAST))


# Matches a </pre> end token
pre_end_re = re.compile(r"(?i)<\s*/\s*pre\s*>")

# Matches a list item prefix
list_prefix_re = re.compile(r"[*:;#]+")

# Dictionary mapping fixed form tokens to their handler functions.
# Tokens that have variable form are handled in the code in token_iter().
tokenops = {
    "'''": bold_fn,
    "''": italic_fn,
    "[": elink_start_fn,
    "]": elink_end_fn,
    "{|": table_start_fn,
    "|}": table_end_fn,
    "|+": table_caption_fn,
    "!": table_hdr_cell_fn,
    "!!": table_hdr_cell_fn,
    "|-": table_row_fn,
    "||": double_vbar_fn,
    "|": vbar_fn,
    # The following are here only because it speeds up operations over handling
    # them in the general way (by about 10% in overall parsing speed)
    " ": text_fn,
    "\n": text_fn,
    "\t": text_fn,
    "\n\n": text_fn,
}
for x in MAGIC_WORDS:
    tokenops[x] = magicword_fn


def token_iter(ctx, text):
    """Tokenizes MediaWiki page content.  This yields (is_token, text) for
    each token.  ``is_token`` is False for text and True for other tokens."""
    assert isinstance(text, str)
    pos = 0
    for m in re.finditer(token_re, text):
        start = m.start()
        if pos != start:
            yield False, text[pos:start]
        pos = m.end()
        token = m.group(0)
        if token.startswith("=="):
            yield True, "<" + m.group(1)
            for x in token_iter(ctx, m.group(2)):
                yield x
            yield True, ">" + m.group(4)
        else:
            yield True, token
    if pos != len(text):
        yield False, text[pos:]


def nowiki_sub_fn(m):
    """This function escapes the contents of a <nowiki> ... </nowiki> pair."""
    text = m.group(1)
    text = re.sub(r";", "&semi;", text)
    text = html.escape(text, quote=True)
    text = re.sub(r"=", "&equals;", text)
    text = re.sub(r"\*", "&ast;", text)
    text = re.sub(r"#", "&num;", text)
    text = re.sub(r":", "&colon;", text)
    text = re.sub(r"!", "&excl;", text)
    text = re.sub(r"\|", "&vert;", text)
    text = re.sub(r"\[", "&lsqb;", text)
    text = re.sub(r"\]", "&rsqb;", text)
    text = re.sub(r"\{", "&lbrace;", text)
    text = re.sub(r"\}", "&rbrace;", text)
    text = re.sub(r"\s+", " ", text)
    return text


def preprocess_text(text):
    # XXX this will be eliminated, needs to be handled differently
    assert isinstance(text, str)
    text = re.sub(r"(?si)<\s*nowiki\s*>(.*?)<\s*/\s*nowiki\s*>", nowiki_sub_fn,
                  text)
    text = re.sub(r"(?si)<\s*nowiki\s*/\s*>", MAGIC_NOWIKI_CHAR, text)
    text = re.sub(r"(?s)<!\s*--.*?--\s*>", "", text)
    return text


def process_text(ctx, text):
    """Tokenizes ``text`` and processes each token in sequence.  This can be
    called recursively (which we do to process tokens inside templates and
    certain other structures)."""
    for is_token, token in token_iter(ctx, text):
        node = ctx.stack[-1]
        if not is_token:
            # Process it as normal text.
            text_fn(ctx, token)
        elif (node.kind == NodeKind.PRE and not re.match(pre_end_re, token)):
            # Remove the artificially added prefix from subtitle tokens.
            # Then process the token as normal text as we are in a
            # non-interpreting context.
            if token.startswith("<=="):
                token = token[1:]
            elif token.startswith(">=="):
                token = token[1:]
            text_fn(ctx, token)
        else:
            # Process it as a token.  In some contexts some tokens may still
            # be interpreted as text.
            if token in tokenops:
                tokenops[token](ctx, token)
            elif token.startswith("<=="):  # Note: < added by tokenizer
                subtitle_start_fn(ctx, token)
            elif token.startswith(">=="):  # Note: > added by tokenizer
                subtitle_end_fn(ctx, token)
            elif token.startswith("<") and len(token):
                tag_fn(ctx, token)
            elif token.startswith("----"):
                hline_fn(ctx, token)
            elif re.match(list_prefix_re, token):
                list_fn(ctx, token)
            elif token.startswith("https://") or token.startswith("http://"):
                url_fn(ctx, token)
            elif (len(token) == 1 and ord(token) >= MAGIC_FIRST and
                  ord(token) <= MAGIC_LAST):
                magic_fn(ctx, token)
            else:
                text_fn(ctx, token)
        ctx.linenum += token.count("\n")
        ctx.beginning_of_line = token[-1] == "\n"



def parse_encoded(ctx, text):
    # XXX comment
    assert ctx.title is not None  # ctx.start_page() must have been called
    node = WikiNode(NodeKind.ROOT, 0)
    node.args.append([ctx.title])
    ctx.beginning_of_line = True
    ctx.linenum = 1
    ctx.pre_parse = False
    ctx.stack = [node]
    ctx.suppress_special = False



    # Process all tokens from the input.
    process_text(ctx, text)

    # We are at the end of the text.  Keep popping stack until we only have
    # the root node left.  This is used to finalize processing any nodes
    # on the stack.
    while True:
        node = ctx.stack[-1]
        if node.kind == NodeKind.ROOT:
            break
        _parser_pop(ctx, True)
    assert len(ctx.stack) == 1
    # If the last children are strings, merge them to one string.
    _parser_merge_str_children(ctx)
    return ctx.stack[0]

def print_tree(tree, indent=0):
    """Prints the parse tree for debugging purposes.  This does not expand
    HTML entities; that should be done after processing templates."""
    assert isinstance(tree, (WikiNode, str))
    assert isinstance(indent, int)
    if isinstance(tree, str):
        print("{}{}".format(" " * indent, repr(tree)))
        return
    print("{}{} {}".format(" " * indent, tree.kind.name, tree.args))
    for k, v in tree.attrs.items():
        print("{}    {}={}".format(" " * indent, k, v))
    for child in tree.children:
        print_tree(child, indent + 2)


# Very simple test:
# data = open("pages/Words/ho/horse.txt", "r").read()
# tree = parse("horse", data)
# print_tree(tree, 0)
