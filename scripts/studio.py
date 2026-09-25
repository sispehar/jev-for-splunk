"""A small builder for Dashboard Studio views (JSON inside the XML wrapper Splunk reads from default/data/ui/views).

Supports chained data sources, event annotations (a secondary data source named "annotation" with
_time, annotation_label, annotation_color) and a submit button.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict

WIDTH = 1440
GAP = 12


def _xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Dashboard(object):
    def __init__(self, name, label, title, description, time_default="-7d@h,now", time_input=True, submit=False):
        self.name = name
        self.label = label
        self.title = title
        self.description = description
        self.submit = submit
        self.inputs = OrderedDict()
        self.global_inputs = []
        self.visualizations = OrderedDict()
        self.data_sources = OrderedDict()
        self.structure = []
        self.y = 0
        if time_input:
            self.add_input("input_tr", {"type": "input.timerange", "title": "Time range",
                                        "options": {"token": "tr", "defaultValue": time_default}})

    # -- inputs -----------------------------------------------------------------
    def add_input(self, key, definition):
        self.inputs[key] = definition
        self.global_inputs.append(key)

    def text_input(self, token, title, default=""):
        self.add_input("input_" + token, {"type": "input.text", "title": title,
                                          "options": {"token": token, "defaultValue": default}})

    def dropdown(self, token, title, items, default):
        self.add_input("input_" + token, {"type": "input.dropdown", "title": title,
                                          "options": {"token": token, "defaultValue": default, "items": items}})

    # -- data sources -------------------------------------------------------------
    def ds(self, key, query, timed=True, earliest=None, latest=None):
        options = OrderedDict([("query", query)])
        if timed:
            options["queryParameters"] = {"earliest": earliest or "$tr.earliest$", "latest": latest or "$tr.latest$"}
        self.data_sources[key] = {"type": "ds.search", "name": key, "options": options}
        return key

    def chain(self, key, extend, query):
        if not query.lstrip().startswith("|"):
            raise ValueError("a chained search must start with |: %s" % key)
        self.data_sources[key] = {"type": "ds.chain", "name": key, "options": {"extend": extend, "query": query}}
        return key

    # -- visualizations -------------------------------------------------------------
    def viz(self, key, vtype, title, ds_key=None, options=None, description=None, annotation=None):
        item = OrderedDict([("type", vtype)])
        if title:
            item["title"] = title
        if description:
            item["description"] = description
        if options:
            item["options"] = options
        if ds_key:
            item["dataSources"] = OrderedDict([("primary", ds_key)])
            if annotation:
                item["dataSources"]["annotation"] = annotation
        self.visualizations[key] = item
        return key

    def single(self, key, title, ds_key, unit=None, color=None, description=None, precision=0, sparkline=False):
        options = {"majorValue": "> primary | seriesByName('value') | lastPoint()", "trendDisplay": "off",
                   "sparklineDisplay": "below" if sparkline else "off", "numberPrecision": precision}
        if sparkline:
            options["sparklineValues"] = "> primary | seriesByName('value')"
        if unit:
            options["unit"] = unit
        if color:
            options["majorColor"] = color
        return self.viz(key, "splunk.singlevalue", title, ds_key, options, description)

    def table(self, key, title, ds_key, options=None, description=None):
        base = {"count": 15, "showInternalFields": False}
        base.update(options or {})
        return self.viz(key, "splunk.table", title, ds_key, base, description)

    def markdown(self, key, text, font_size="small"):
        return self.viz(key, "splunk.markdown", None, None, {"markdown": text, "fontSize": font_size})

    # -- layout -----------------------------------------------------------------------
    def row(self, keys, height):
        n = len(keys)
        w = int((WIDTH - GAP * (n - 1)) / n)
        x = 0
        for key in keys:
            self.structure.append({"item": key, "type": "block", "position": {"x": x, "y": self.y, "w": w, "h": height}})
            x += w + GAP
        self.y += height + GAP

    def row_weighted(self, items, height):
        total = float(sum(w for _, w in items))
        x = 0
        for key, weight in items:
            w = int((WIDTH - GAP * (len(items) - 1)) * weight / total)
            self.structure.append({"item": key, "type": "block", "position": {"x": x, "y": self.y, "w": w, "h": height}})
            x += w + GAP
        self.y += height + GAP

    # -- output -----------------------------------------------------------------------
    def definition(self):
        defaults = {"dataSources": {"ds.search": {"options": {"queryParameters": {
            "earliest": "$tr.earliest$", "latest": "$tr.latest$"}}}}} if "input_tr" in self.inputs else {"dataSources": {}}
        layout = OrderedDict([("type", "grid"), ("options", OrderedDict([("width", WIDTH), ("height", self.y)])),
                              ("globalInputs", self.global_inputs), ("structure", self.structure)])
        if self.submit:
            layout["options"]["submitButton"] = True
        return OrderedDict([
            ("title", self.title), ("description", self.description), ("inputs", self.inputs), ("defaults", defaults),
            ("visualizations", self.visualizations), ("dataSources", self.data_sources), ("layout", layout),
        ])

    def xml(self):
        body = json.dumps(self.definition(), indent=4, ensure_ascii=False)
        return ('<dashboard version="2" theme="dark">\n    <label>%s</label>\n    <description>%s</description>\n'
                '    <definition><![CDATA[%s]]></definition>\n</dashboard>\n') % (_xml(self.label), _xml(self.description), body)

    def write(self, views_dir):
        path = os.path.join(views_dir, self.name + ".xml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.xml())
        return path
