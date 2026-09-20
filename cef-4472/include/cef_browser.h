// Minimal CEF header shim -- see cef_base.h for rationale.
//
// The bridge never calls CefBrowser/CefBrowserHost methods; it resolves the
// host object from raw memory and dispatches through vtable slots by index.
// But CefRefPtr<T> appears by value in signatures, so its instantiation needs
// complete types. These stubs exist only to satisfy that; none of their
// virtuals is ever invoked.

#ifndef AWOO_CEF_SHIM_CEF_BROWSER_H_
#define AWOO_CEF_SHIM_CEF_BROWSER_H_

#include "include/cef_base.h"
#include "include/cef_registration.h"
#include "include/cef_devtools_message_observer.h"

class CefBrowser : public virtual CefBaseRefCounted {};
class CefBrowserHost : public virtual CefBaseRefCounted {};
class CefClient : public virtual CefBaseRefCounted {};
class CefDictionaryValue : public virtual CefBaseRefCounted {};
class CefFrame : public virtual CefBaseRefCounted {};
class CefNavigationEntry : public virtual CefBaseRefCounted {};
class CefNavigationEntryVisitor : public virtual CefBaseRefCounted {};
class CefRequestContext : public virtual CefBaseRefCounted {};

#endif  // AWOO_CEF_SHIM_CEF_BROWSER_H_
