// Minimal CEF header shim for building AwooNcmCefBridge.dll.
//
// The bridge drives libcef purely through raw ABI: it validates the target's
// CEF version and API hashes, then reads/patches vtables by index and invokes
// function pointers directly. It therefore needs almost nothing from the real
// CEF headers -- only type identity and the observer interface layout, which
// MUST match upstream exactly because the bridge patches vtable slots 0..4 of
// its observer object.
//
// Layout verified against chromiumembedded/cef include/:
//   CefBaseRefCounted        : AddRef, Release, HasOneRef, HasAtLeastOneRef (4 vfns)
//   CefDevToolsMessageObserver : OnDevToolsMessage, OnDevToolsMethodResult,
//                                OnDevToolsEvent, OnDevToolsAgentAttached,
//                                OnDevToolsAgentDetached                 (5 vfns)
//
// The target's libcef.dll reports the exact API hashes this bridge was built
// against, which guarantees the upstream vtable/struct layout is in effect.

#ifndef AWOO_CEF_SHIM_CEF_BASE_H_
#define AWOO_CEF_SHIM_CEF_BASE_H_

#include <atomic>
#include <cstddef>

// ---------------------------------------------------------------------------
// Refcounted string. Only ever used by reference in thunk signatures here.
// ---------------------------------------------------------------------------
class CefString {
 public:
  CefString() = default;
  CefString(const CefString&) = delete;
  CefString& operator=(const CefString&) = delete;
};

// ---------------------------------------------------------------------------
// Atomic refcount (mirrors CEF's CefRefCount).
// ---------------------------------------------------------------------------
class CefRefCount {
 public:
  CefRefCount() = default;
  CefRefCount(const CefRefCount&) = delete;
  CefRefCount& operator=(const CefRefCount&) = delete;

  void AddRef() const { ref_count_.fetch_add(1, std::memory_order_relaxed); }

  bool Release() const {
    return ref_count_.fetch_sub(1, std::memory_order_acq_rel) == 1;
  }

  bool HasOneRef() const {
    return ref_count_.load(std::memory_order_acquire) == 1;
  }

  bool HasAtLeastOneRef() const {
    return ref_count_.load(std::memory_order_acquire) >= 1;
  }

 private:
  mutable std::atomic<int> ref_count_{0};
};

// ---------------------------------------------------------------------------
// The four pure virtual refcount methods define vtable slots 0..3.
// ---------------------------------------------------------------------------
class CefBaseRefCounted {
 public:
  virtual void AddRef() const = 0;
  virtual bool Release() const = 0;
  virtual bool HasOneRef() const = 0;
  virtual bool HasAtLeastOneRef() const = 0;

 protected:
  virtual ~CefBaseRefCounted() {}
};

// ---------------------------------------------------------------------------
// scoped_refptr stand-in. The bridge only needs construction, copying and
// get(); it never dereferences through it.
// ---------------------------------------------------------------------------
template <class T>
class CefRefPtr {
 public:
  CefRefPtr() noexcept : ptr_(nullptr) {}

  CefRefPtr(T* p) : ptr_(p) {  // NOLINT(runtime/explicit) - CEF behaves so
    if (ptr_) {
      ptr_->AddRef();
    }
  }

  template <class U>
  CefRefPtr(const CefRefPtr<U>& that) : ptr_(that.get()) {
    if (ptr_) {
      ptr_->AddRef();
    }
  }

  CefRefPtr(const CefRefPtr& that) : ptr_(that.ptr_) {
    if (ptr_) {
      ptr_->AddRef();
    }
  }

  ~CefRefPtr() {
    if (ptr_) {
      ptr_->Release();
    }
  }

  CefRefPtr& operator=(const CefRefPtr& that) {
    if (ptr_ != that.ptr_) {
      T* old = ptr_;
      ptr_ = that.ptr_;
      if (ptr_) {
        ptr_->AddRef();
      }
      if (old) {
        old->Release();
      }
    }
    return *this;
  }

  CefRefPtr& operator=(T* p) {
    if (ptr_ != p) {
      T* old = ptr_;
      ptr_ = p;
      if (ptr_) {
        ptr_->AddRef();
      }
      if (old) {
        old->Release();
      }
    }
    return *this;
  }

  T* get() const { return ptr_; }
  T* operator->() const { return ptr_; }
  explicit operator bool() const { return ptr_ != nullptr; }

 private:
  T* ptr_;
};

#endif  // AWOO_CEF_SHIM_CEF_BASE_H_
