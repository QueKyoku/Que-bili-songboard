// Minimal CEF header shim -- see cef_base.h for rationale.
//
// The observer interface MUST keep this exact virtual-method order: the bridge
// overrides vtable slots 0..4 of the refcount base and keeps slots 5..9 from
// the derived class, then hands the object to libcef. Getting this wrong would
// mis-dispatch every DevTools callback.

#ifndef AWOO_CEF_SHIM_CEF_DEVTOOLS_MESSAGE_OBSERVER_H_
#define AWOO_CEF_SHIM_CEF_DEVTOOLS_MESSAGE_OBSERVER_H_

#include "include/cef_base.h"

class CefBrowser;

class CefDevToolsMessageObserver : public virtual CefBaseRefCounted {
 public:
  virtual bool OnDevToolsMessage(CefRefPtr<CefBrowser> browser,
                                 const void* message,
                                 size_t message_size) {
    return false;
  }

  virtual void OnDevToolsMethodResult(CefRefPtr<CefBrowser> browser,
                                      int message_id,
                                      bool success,
                                      const void* result,
                                      size_t result_size) {}

  virtual void OnDevToolsEvent(CefRefPtr<CefBrowser> browser,
                               const CefString& method,
                               const void* params,
                               size_t params_size) {}

  virtual void OnDevToolsAgentAttached(CefRefPtr<CefBrowser> browser) {}

  virtual void OnDevToolsAgentDetached(CefRefPtr<CefBrowser> browser) {}
};

#endif  // AWOO_CEF_SHIM_CEF_DEVTOOLS_MESSAGE_OBSERVER_H_
