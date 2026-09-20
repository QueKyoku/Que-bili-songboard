// Minimal CEF header shim -- see cef_base.h for rationale.

#ifndef AWOO_CEF_SHIM_CEF_REGISTRATION_H_
#define AWOO_CEF_SHIM_CEF_REGISTRATION_H_

#include "include/cef_base.h"

class CefRegistration : public virtual CefBaseRefCounted {};

#endif  // AWOO_CEF_SHIM_CEF_REGISTRATION_H_
