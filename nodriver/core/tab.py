from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import typing
import warnings
from typing import List, Optional, Tuple, Union

import nodriver.core.browser

from .. import cdp
from . import element, util
from .config import PathLike
from .connection import Connection, ProtocolException, Transaction
import inspect

logger = logging.getLogger(__name__)


def camel_to_snake(name):
    """Converts a CamelCase string to snake_case."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


class Tab(Connection):
    """
    :ref:`tab` is the controlling mechanism/connection to a 'target',
    for most of us 'target' can be read as 'tab'. however it could also
    be an iframe, serviceworker or background script for example,
    although there isn't much to control for those.

    if you open a new window by using :py:meth:`browser.get(..., new_window=True)`
    your url will open a new window. this window is a 'tab'.
    When you browse to another page, the tab will be the same (it is an browser view).

    So it's important to keep some reference to tab objects, in case you're
    done interacting with elements and want to operate on the page level again.

    Custom CDP commands
    ---------------------------
    Tab object provide many useful and often-used methods. It is also
    possible to utilize the included cdp classes to do something totally custom.

    The cdp package is a set of so-called "domains" with each having methods, events and types.
    To send a cdp method, for example :py:obj:`cdp.page.navigate`, you'll have to check
    whether the method accepts any parameters and whether they are required or not.

    You can use:

    ```python
    await tab.send(cdp.page.navigate(url='https://yoururlhere'))
    ```

    So tab.send() accepts a generator object, which is created by calling a cdp method.
    This way you can build very detailed and customized commands.
    (Note: finding correct command combo's can be a time consuming task, luckily I added a whole bunch
    of useful methods, preferably having the same api's or lookalikes, as in selenium)


    Some useful, often needed and simply required methods
    ===================================================================


    :py:meth:`~find`  |  find(text)
    ----------------------------------------
    Find and returns a single element by text match. By default returns the first element found.
    Much more powerful is the best_match flag, although also much more expensive.
    When no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~find` |  find(text, best_match=True) or find(text, True)
    ---------------------------------------------------------------------------------
    Much more powerful (and expensive!!) than the above, is the use of `find(text, best_match=True)` flag.
    It will still return 1 element, but when multiple matches are found, picks the one having the
    most similar text length.
    How would that help?
    For example, you search for "login", you'd probably want the "login" button element,
    and not thousands of scripts, meta, headings which happens to contain a string of "login".

    When no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~select` | select(selector)
    ----------------------------------------
    Find and returns a single element by css selector match.
    When no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    :py:meth:`~select_all` | select_all(selector)
    ------------------------------------------------
    Find and returns all elements by css selector match.
    When no match is found, it will retry for <timeout> seconds (default: 10), so
    this is also suitable to use as wait condition.


    await :py:obj:`Tab`
    ---------------------------
    Calling `await tab` will do a lot of stuff under the hood, and ensures all references
    are up to date. Also, it allows for the script to "breathe", as it is often faster than your browser or
    webpage. So whenever you get stuck and things crashes or element could not be found, you should probably let
    it "breathe"  by calling `await page`  and/or `await page.sleep()`

    Also, it's ensuring :py:obj:`~url` will be updated to the most recent one, which is quite important in some
    other methods.

    Using other and custom CDP commands
    ======================================================
    Using the included cdp module, you can easily craft commands, which will always return a generator object.
    This generator object can be easily sent to the :py:meth:`~send`  method.

    :py:meth:`~send`
    ---------------------------
    This is probably THE most important method, although you won't ever call it, unless you want to
    go really custom. The send method accepts a :py:obj:`cdp` command. Each of which can be found in the
    cdp section.

    When you import * from this package, cdp will be in your namespace, and contains all domains/actions/events
    you can act upon.
    """

    browser: nodriver.core.browser.Browser
    _download_behavior: List[str] = None

    def __init__(
        self,
        websocket_url: str,
        target: cdp.target.TargetInfo,
        browser: Optional["nodriver.Browser"] = None,
        **kwargs,
    ):
        super().__init__(websocket_url, target, browser, **kwargs)
        self.browser = browser
        self._dom = None
        self._window_id = None

    async def send_cdp(self, command_dict):
        """
        Sends a raw CDP command to this tab.  Handles domain enabling.

        Args:
            command_dict: A dictionary with "method" and "params" keys.
                           e.g., {"method": "Page.captureScreenshot", "params": {"format": "jpeg"}}
        """
        await self.aopen()  # Ensure the WebSocket is open

        method_name = command_dict.get("method")
        params = command_dict.get("params", {})  # Default to an empty dict

        if not method_name:
            raise ValueError("command_dict must contain a 'method' key.")

        # --- Construct the CDP object dynamically ---
        domain_name, method_short_name = method_name.split(".")
        domain_mod = getattr(cdp, domain_name.lower())
        method_short_name = camel_to_snake(method_short_name)
        method_func = getattr(domain_mod, method_short_name)

        # --- Inspect the method's parameters and validate ---
        sig = inspect.signature(method_func)
        bound_args = sig.bind(**params)
        bound_args.apply_defaults()

        # Create the CDP object (generator)
        cdp_obj = method_func(*bound_args.args, **bound_args.kwargs)

        # --- Enable necessary domains (if they have an enable method) ---
        if domain_mod not in self.enabled_domains:
            if hasattr(domain_mod, 'enable'):  # Check for 'enable' method
                try:
                    self.enabled_domains.append(domain_mod)
                    await self.send(domain_mod.enable(), _is_update=True)
                except Exception as e:
                    self.enabled_domains.remove(domain_mod)
                    logger.error(f"Failed to enable domain {domain_mod}: {e}")
                    raise  # Re-raise after logging

        # --- Use Transaction to send the command and await the result ---
        tx = Transaction(cdp_obj)
        tx.connection = self
        tx.id = next(self.__count__)
        self.mapper.update({tx.id: tx})
        await self.websocket.send(tx.message)
        try:
            return await tx  # Await the result (or raise an exception)
        except ProtocolException as e:
            e.message += f"\ncommand:{tx.method}\nparams:{tx.params}"
            raise e

    @property
    def inspector_url(self):
        """
        Get the inspector URL. This URL can be used in another browser to show you the DevTools interface for
        the current tab. Useful for debugging (and headless).
        """
        return (
            f"http://{self.browser.config.host}:{self.browser.config.port}"
            f"/devtools/inspector.html?ws={self.websocket_url[5:]}"
        )

    def inspector_open(self):
        """Open the inspector URL in a new browser window."""
        import webbrowser

        webbrowser.open(self.inspector_url, new=2)

    async def open_external_inspector(self):
        """Open the DevTools inspector page for this tab in the system's browser."""
        import webbrowser

        webbrowser.open(self.inspector_url)

    async def find(
        self,
        text: str,
        best_match: bool = True,
        return_enclosing_element=True,
        timeout: Union[int, float] = 10,
    ):
        """
        Find a single element by text. Can also be used to wait for such an element to appear.

        :param text: Text to search for.  Note: script contents are also considered text.
        :param best_match: When True (default), it will return the element with the most
                           comparable string length. This helps when searching for, e.g., "login"
                           (you probably want the login button, not scripts containing "login").
                           When False, it returns the first match (faster).
        :param return_enclosing_element: Usually returns the *containing* element of a text node.
                                         Set to False if you want the text node itself.
        :param timeout: Raise a timeout exception after this many seconds if nothing is found.
        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        text = text.strip()

        item = await self.find_element_by_text(text, best_match, return_enclosing_element)
        while not item:
            await self
            item = await self.find_element_by_text(text, best_match, return_enclosing_element)
            if loop.time() - start_time > timeout:
                return item  # Return None if not found within timeout
            await self.sleep(0.5)
        return item

    async def select(self, selector: str, timeout: Union[int, float] = 10) -> nodriver.Element:
        """
        Find a single element by CSS selector.  Can also be used to wait for an element.

        :param selector: CSS selector, e.g., a[href], button[class*=close], a > img[src]
        :param timeout: Raise a timeout exception after this many seconds if nothing is found.
        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        selector = selector.strip()
        item = await self.query_selector(selector)

        while not item:
            await self
            item = await self.query_selector(selector)
            if loop.time() - start_time > timeout:
                return item  # Return None
            await self.sleep(0.5)
        return item

    async def find_all(
        self, text: str, timeout: Union[int, float] = 10
    ) -> List[nodriver.Element]:
        """
        Find multiple elements by text.  Can be used as a wait condition.

        :param text: Text to search for.  Note: script contents are also considered.
        :param timeout: Raise a timeout exception after this many seconds.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        text = text.strip()
        items = await self.find_elements_by_text(text)

        while not items:
            await self
            items = await self.find_elements_by_text(text)
            if loop.time() - now > timeout:
                return items  # Return empty list
            await self.sleep(0.5)
        return items

    async def select_all(
        self, selector: str, timeout: Union[int, float] = 10, include_frames=False
    ) -> List[nodriver.Element]:
        """
        Find multiple elements by CSS selector. Can be used as a wait condition.

        :param selector: CSS selector, e.g., a[href], button[class*=close]
        :param timeout: Raise a timeout exception after this many seconds.
        :param include_frames: Whether to include results in iframes.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        selector = selector.strip()
        items = []
        if include_frames:
            frames = await self.query_selector_all("iframe")
            for fr in frames:
                items.extend(await fr.query_selector_all(selector))

        items.extend(await self.query_selector_all(selector))
        while not items:
            await self
            items = await self.query_selector_all(selector)
            if loop.time() - now > timeout:
                return items  # Return empty list
            await self.sleep(0.5)
        return items

    async def get(
        self, url="chrome://welcome", new_tab: bool = False, new_window: bool = False
    ):
        """
        Navigate to a URL.  Handles waits and DOM events for safer navigation.

        :param url: The URL to navigate to.
        :param new_tab: Open a new tab.
        :param new_window: Open a new window (implies new_tab=True).
        """
        if not self.browser:
            raise AttributeError("This Tab has no 'browser' attribute, so get() cannot be used.")
        if new_window and not new_tab:
            new_tab = True

        if new_tab:
            return await self.browser.get(url, new_tab, new_window)
        else:
            frame_id, loader_id, *_ = await self.send(cdp.page.navigate(url))
            await self  # Wait for page load
            return self

    async def query_selector_all(
        self,
        selector: str,
        _node: Optional[Union[cdp.dom.Node, "element.Element"]] = None,
    ) -> List[nodriver.Element]:
        """
        Equivalent of JavaScript's document.querySelectorAll.  Returns all matching Elements.

        :param selector: CSS selector.
        :param _node: Internal use (for nested queries).
        """
        if not _node:
            doc: cdp.dom.Node = await self.send(cdp.dom.get_document(-1, True))
        else:
            doc = _node
            if _node.node_name == "IFRAME":
                doc = _node.content_document
        node_ids = []

        try:
            node_ids = await self.send(cdp.dom.query_selector_all(doc.node_id, selector))
        except ProtocolException as e:
            if _node is not None:
                if "could not find node" in e.message.lower():
                    if getattr(_node, "__last", None):  # Prevent infinite recursion
                        del _node.__last
                        return []
                    await _node.update()
                    _node.__last = True
                    return await self.query_selector_all(selector, _node)
            else:
                await self.send(cdp.dom.disable())  # Clean up on error
                raise
        if not node_ids:
            return []
        items = []

        for nid in node_ids:
            node = util.filter_recurse(doc, lambda n: n.node_id == nid)
            if not node:
                continue
            elem = element.create(node, self, doc)  # Pass the document for efficiency
            items.append(elem)
        return items

    async def query_selector(
        self,
        selector: str,
        _node: Optional[Union[cdp.dom.Node, element.Element]] = None,
    ) -> Optional[nodriver.Element]:
        """
        Find a single element by CSS selector.

        :param selector: CSS selector string.
        :param _node: Internal use (for nested queries).
        """
        selector = selector.strip()

        if not _node:
            doc: cdp.dom.Node = await self.send(cdp.dom.get_document(-1, True))
        else:
            doc = _node
            if _node.node_name == "IFRAME":
                doc = _node.content_document
        node_id = None

        try:
            node_id = await self.send(cdp.dom.query_selector(doc.node_id, selector))
        except ProtocolException as e:
            if _node is not None:
                if "could not find node" in e.message.lower():
                    if getattr(_node, "__last", None):
                        del _node.__last
                        return None
                    await _node.update()
                    _node.__last = True
                    return await self.query_selector(selector, _node)
            else:
                await self.send(cdp.dom.disable())
                raise
        if not node_id:
            return None
        node = util.filter_recurse(doc, lambda n: n.node_id == node_id)
        if not node:
            return None
        return element.create(node, self, doc)

    async def find_elements_by_text(
        self, text: str, tag_hint: Optional[str] = None
    ) -> List[element.Element]:
        """
        Return elements matching the given text.  Includes script contents.

        :param text: The text to search for.
        :param tag_hint: (Optional) Narrow down the search to elements with this tag.
        """
        text = text.strip()
        doc = await self.send(cdp.dom.get_document(-1, True))
        search_id, nresult = await self.send(cdp.dom.perform_search(text, True))
        if nresult:
            node_ids = await self.send(cdp.dom.get_search_results(search_id, 0, nresult))
        else:
            node_ids = []

        await self.send(cdp.dom.discard_search_results(search_id))

        items = []
        for nid in node_ids:
            node = util.filter_recurse(doc, lambda n: n.node_id == nid)
            if not node:
                node = await self.send(cdp.dom.resolve_node(node_id=nid))
                if not node:
                    continue
            try:
                elem = element.create(node, self, doc)
            except:  # noqa  # Broad exception is okay here
                continue
            if elem.node_type == 3:
                # Return the parent element of text nodes (usually what's wanted)
                if not elem.parent:
                    await elem.update()
                items.append(elem.parent or elem)
            else:
                items.append(elem)

        # Search iframes (since we already fetched the whole doc)
        iframes = util.filter_recurse_all(doc, lambda node: node.node_name == "IFRAME")
        if iframes:
            iframes_elems = [
                element.create(iframe, self, iframe.content_document) for iframe in iframes
            ]
            for iframe_elem in iframes_elems:
                if iframe_elem.content_document:
                    iframe_text_nodes = util.filter_recurse_all(
                        iframe_elem,
                        lambda node: node.node_type == 3
                                     and text.lower() in node.node_value.lower(),
                    )
                    if iframe_text_nodes:
                        iframe_text_elems = [
                            element.create(text_node, self, iframe_elem.tree)
                            for text_node in iframe_text_nodes
                        ]
                        items.extend(
                            text_node.parent for text_node in iframe_text_elems
                        )
        await self.send(cdp.dom.disable())
        return items or []

    async def find_element_by_text(
        self,
        text: str,
        best_match: Optional[bool] = False,
        return_enclosing_element: Optional[bool] = True,
    ) -> Union[element.Element, None]:
        """
        Find and return the first element containing <text>, or the best match.

        :param text: The text to search for.
        :param best_match: If True, finds the closest match based on length (more expensive).
        :param return_enclosing_element: If True (default), return the enclosing element, not the text node.
        """
        doc = await self.send(cdp.dom.get_document(-1, True))
        text = text.strip()
        search_id, nresult = await self.send(cdp.dom.perform_search(text, True))

        node_ids = await self.send(cdp.dom.get_search_results(search_id, 0, nresult))
        await self.send(cdp.dom.discard_search_results(search_id))

        if not node_ids:
            node_ids = []
        items = []
        for nid in node_ids:
            node = util.filter_recurse(doc, lambda n: n.node_id == nid)
            try:
                elem = element.create(node, self, doc)
            except:  # noqa
                continue
            if elem.node_type == 3:
                if not elem.parent:
                    await elem.update()
                items.append(elem.parent or elem)
            else:
                items.append(elem)

        # Search iframes
        iframes = util.filter_recurse_all(doc, lambda node: node.node_name == "IFRAME")
        if iframes:
            iframes_elems = [
                element.create(iframe, self, iframe.content_document) for iframe in iframes
            ]
            for iframe_elem in iframes_elems:
                iframe_text_nodes = util.filter_recurse_all(
                    iframe_elem,
                    lambda node: node.node_type == 3
                                 and text.lower() in node.node_value.lower(),
                )
                if iframe_text_nodes:
                    iframe_text_elems = [
                        element.create(text_node, self, iframe_elem.tree)
                        for text_node in iframe_text_nodes
                    ]
                    items.extend(text_node.parent for text_node in iframe_text_elems)

        try:
            if not items:
                return None
            if best_match:
                closest_by_length = min(
                    items, key=lambda el: abs(len(text) - len(el.text_all))
                )
                elem = closest_by_length or items[0]
                return elem
            else:
                for elem in items:
                    if elem:
                        return elem
        finally:
            await self.send(cdp.dom.disable())

    async def back(self):
        """Go back in history."""
        await self.send(cdp.runtime.evaluate("window.history.back()"))

    async def forward(self):
        """Go forward in history."""
        await self.send(cdp.runtime.evaluate("window.history.forward()"))

    async def reload(
        self,
        ignore_cache: Optional[bool] = True,
        script_to_evaluate_on_load: Optional[str] = None,
    ):
        """
        Reload the page.

        :param ignore_cache: If True (default), ignore the cache.
        :param script_to_evaluate_on_load: Script to run on load (not fully tested).
        """
        await self.send(
            cdp.page.reload(
                ignore_cache=ignore_cache,
                script_to_evaluate_on_load=script_to_evaluate_on_load,
            ),
        )

    async def evaluate(
        self, expression: str, await_promise=False, return_by_value=True
    ):
        """
        Evaluate a JavaScript expression.

        :param expression: The expression to evaluate.
        :param await_promise: Whether to await a Promise.
        :param return_by_value: Whether to return the result by value.
        """
        remote_object, errors = await self.send(
            cdp.runtime.evaluate(
                expression=expression,
                user_gesture=True,
                await_promise=await_promise,
                return_by_value=return_by_value,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if errors:
            raise ProtocolException(errors)

        if remote_object:
            if return_by_value:
                if remote_object.value:
                    return remote_object.value
            else:
                return remote_object, errors
        return None

    async def js_dumps(self, obj_name: str, return_by_value: Optional[bool] = True):
        """
        Dump a JavaScript object with its properties and values as a dict.

        Note: Complex objects might not be serializable.

        :param obj_name: The JS object to dump.
        :param return_by_value: If False, return a tuple of (return value, errors).
        """
        js_code_a = (
            f"""
            function ___dump(obj, _d = 0) {{
                let _typesA = ['object', 'function'];
                let _typesB = ['number', 'string', 'boolean'];
                if (_d == 2) {{
                    console.log('maxdepth reached for ', obj);
                    return;
                }}
                let tmp = {{}};
                for (let k in obj) {{
                    if (obj[k] == window) continue;
                    try {{
                        if (obj[k] === null || obj[k] === undefined || obj[k] === NaN) {{
                            console.log('obj[k] is null or undefined or Nan', k, '=>', obj[k]);
                            tmp[k] = obj[k];
                            continue;
                        }}
                    }} catch (e) {{
                        tmp[k] = null;
                        continue;
                    }}

                    if (_typesB.includes(typeof obj[k])) {{
                        tmp[k] = obj[k];
                        continue;
                    }}

                    try {{
                        if (typeof obj[k] === 'function') {{
                            tmp[k] = obj[k].toString();
                            continue;
                        }}

                        if (typeof obj[k] === 'object') {{
                            tmp[k] = ___dump(obj[k], _d + 1);
                            continue;
                        }}
                    }} catch (e) {{}}

                    try {{
                        tmp[k] = JSON.stringify(obj[k]);
                        continue;
                    }} catch (e) {{}}

                    try {{
                        tmp[k] = obj[k].toString();
                        continue;
                    }} catch (e) {{}}
                }}
                return tmp;
            }}

            function ___dumpY(obj) {{
                var objKeys = (obj) => {{
                    var [target, result] = [obj, []];
                    while (target !== null) {{
                        result = result.concat(Object.getOwnPropertyNames(target));
                        target = Object.getPrototypeOf(target);
                    }}
                    return result;
                }};
                return Object.fromEntries(
                    objKeys(obj).map(_ => [_, ___dump(obj[_])])
                );
            }}
            ___dumpY({obj_name});
            """
        )

        js_code_b = (
            f"""
            ((obj, visited = new WeakSet()) => {{
                if (visited.has(obj)) {{
                    return {{}};
                }}
                visited.add(obj);
                var result = {{}}, _tmp;
                for (var i in obj) {{
                    try {{
                        if (i === 'enabledPlugin' || typeof obj[i] === 'function') {{
                            continue;
                        }} else if (typeof obj[i] === 'object') {{
                            _tmp = recurse(obj[i], visited);
                            if (Object.keys(_tmp).length) {{
                                result[i] = _tmp;
                            }}
                        }} else {{
                            result[i] = obj[i];
                        }}
                    }} catch (error) {{
                        // console.error('Error:', error);
                    }}
                }}
                return result;
            }})({obj_name});
            """
        )

        remote_object, exception_details = await self.send(
            cdp.runtime.evaluate(
                js_code_a,
                await_promise=True,
                return_by_value=return_by_value,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if exception_details:
            # Try the second variant
            remote_object, exception_details = await self.send(
                cdp.runtime.evaluate(
                    js_code_b,
                    await_promise=True,
                    return_by_value=return_by_value,
                    allow_unsafe_eval_blocked_by_csp=True,
                )
            )

        if exception_details:
            raise ProtocolException(exception_details)
        if return_by_value:
            if remote_object.value:
                return remote_object.value
        else:
            return remote_object, exception_details
        return None

    async def close(self):
        """Close the current target (tab, window, page)."""
        if self.target and self.target.target_id:
            await self.send(cdp.target.close_target(target_id=self.target.target_id))

    async def get_window(self) -> Tuple[cdp.browser.WindowID, cdp.browser.Bounds]:
        """Get the window bounds."""
        window_id, bounds = await self.send(cdp.browser.get_window_for_target(self.target_id))
        return window_id, bounds

    async def get_content(self):
        """Get the current page source content (HTML)."""
        doc: cdp.dom.Node = await self.send(cdp.dom.get_document(-1, True))
        return await self.send(cdp.dom.get_outer_html(backend_node_id=doc.backend_node_id))

    async def maximize(self):
        """Maximize the page/tab/window."""
        return await self.set_window_state(state="maximize")

    async def minimize(self):
        """Minimize the page/tab/window."""
        return await self.set_window_state(state="minimize")

    async def fullscreen(self):
        """Set the page/tab/window to fullscreen."""
        return await self.set_window_state(state="fullscreen")

    async def medimize(self):
        """Set the window state to normal."""
        return await self.set_window_state(state="normal")

    async def set_window_size(self, left=0, top=0, width=1280, height=1024):
        """Set the window size and position."""
        return await self.set_window_state(left, top, width, height)

    async def activate(self):
        """Activate this target (tab/window/page)."""
        await self.send(cdp.target.activate_target(self.target.target_id))

    async def bring_to_front(self):
        """Alias for self.activate()."""
        await self.activate()

    async def set_window_state(
        self, left=0, top=0, width=1280, height=720, state="normal"
    ):
        """
        Set the window size or state.

        :param left: Desired offset from left (pixels).
        :param top: Desired offset from the top (pixels).
        :param width: Desired width (pixels).
        :param height: Desired height (pixels).
        :param state: "normal", "fullscreen", "maximized", or "minimized".
        """
        available_states = ["minimized", "maximized", "fullscreen", "normal"]
        window_id: cdp.browser.WindowID
        bounds: cdp.browser.Bounds
        (window_id, bounds) = await self.get_window()

        for state_name in available_states:
            if all(x in state_name for x in state.lower()):
                break
        else:
            raise NameError(
                f"Could not determine any of {','.join(available_states)} from input '{state}'"
            )
        window_state = getattr(
            cdp.browser.WindowState, state_name.upper(), cdp.browser.WindowState.NORMAL
        )
        if window_state == cdp.browser.WindowState.NORMAL:
            bounds = cdp.browser.Bounds(left, top, width, height, window_state)
        else:
            # min, max, full can only be used when current state == NORMAL
            # therefore we first switch to NORMAL
            await self.set_window_state(state="normal")
            bounds = cdp.browser.Bounds(window_state=window_state)

        await self.send(cdp.browser.set_window_bounds(window_id, bounds=bounds))

    async def scroll_down(self, amount=25):
        """
        Scrolls down the page.

        :param amount: Percentage of the viewport height to scroll.  25 scrolls 1/4 of the page.
        """
        window_id, bounds = await self.get_window()
        await self.send(
            cdp.input_.synthesize_scroll_gesture(
                x=bounds.left + (bounds.width // 2),  # Scroll from the center of the window
                y=bounds.top + (bounds.height // 2),
                y_distance=-(bounds.height * (amount / 100)),  # Negative for down
                prevent_fling=True,
                speed=800,  # Adjust speed as needed
            )
        )

    async def scroll_up(self, amount=25):
        """
        Scrolls up the page.

        :param amount: Percentage of the viewport height to scroll. 25 scrolls 1/4 of the page.
        """
        window_id, bounds = await self.get_window()
        await self.send(
            cdp.input_.synthesize_scroll_gesture(
                x=bounds.left + (bounds.width // 2),  # Scroll from the center of the window
                y=bounds.top + (bounds.height // 2),
                y_distance=(bounds.height * (amount / 100)),  # Positive for up
                prevent_fling=True,
                speed=800,  # Adjust speed as needed
            )
        )
