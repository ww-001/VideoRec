# -*- coding: utf-8 -*-
"""
dshow_enum.py -- 用 COM ICreateDevEnum 枚举 DirectShow 视频采集设备

与 OpenCV CAP_DSHOW / ffmpeg dshow 使用完全相同的系统枚举器
(CoCreateInstance(CLSID_SystemDeviceEnum) + ICreateDevEnum +
IEnumMoniker)，枚举顺序 = OpenCV VideoCapture 索引顺序。

纯 ctypes 实现，零第三方依赖，Win7 (COM 系统组件) 与 Win10 均可用。

返回 [(friendly_name, device_path), ...] 按枚举顺序。
device_path 形如 @device:pnp:\\?\\usb#vid_xxxx&pid_xxxx#...\global
"""
import ctypes
from ctypes import HRESULT, POINTER, byref, c_void_p, c_ulong, c_wchar_p

ole32 = ctypes.OleDLL('ole32')
ole32.CoInitializeEx.restype = HRESULT
ole32.CoCreateInstance.restype = HRESULT
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemFree.argtypes = [c_void_p]

CLSCTX_INPROC_SERVER = 0x1
CLSID_SystemDeviceEnum = '{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}'
IID_ICreateDevEnum = '{29840822-5B84-11D0-BD3B-00A0C911CE86}'
CLSID_VideoInputDeviceCategory = '{860BB310-5D01-11D0-BD3B-00A0C911CE86}'


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]


def _clsid(s):
    from ctypes import create_unicode_buffer
    buf = create_unicode_buffer(s)
    guid = _GUID()
    r = ole32.CLSIDFromString(buf, byref(guid))
    if r != 0:
        raise OSError(f'CLSIDFromString failed: {r}')
    return guid


def _vtbl_call(obj, idx, *args, restype=HRESULT, argtypes=()):
    vtbl = ctypes.cast(obj, POINTER(POINTER(ctypes.c_void_p))).contents
    func = ctypes.cast(vtbl[idx], ctypes.CFUNCTYPE(restype, c_void_p, *argtypes))
    return func(obj, *args)


# IMoniker vtable 索引:
#   0-2 IUnknown, 3 IPersist::GetClassID, 4-7 IPersistStream,
#   8 BindToObject, 9 BindToStorage, ... 20 GetDisplayName
def _get_display_name(moniker):
    """IMoniker::GetDisplayName -> device path string"""
    buf = POINTER(ctypes.c_wchar)()
    hr = _vtbl_call(moniker, 20, None, None, byref(buf),
                    restype=HRESULT,
                    argtypes=[c_void_p, c_void_p, POINTER(POINTER(ctypes.c_wchar))])
    if hr != 0 or not buf:
        return ''
    try:
        return ctypes.wstring_at(buf)
    finally:
        ole32.CoTaskMemFree(buf)


def enum_dshow_video_devices():
    """返回 [(friendly_name, device_path), ...] 按 DirectShow 枚举顺序 (= OpenCV 索引)

    注意: 只读取 moniker 的 GetDisplayName (DevicePath), 不读 FriendlyName。
    BindToStorage/IPropertyBag 在部分系统 (含 Win7) 上可能崩溃, DevicePath
    已含 VID/PID/实例号, 足够与 WMI 交叉匹配出型号名。

    ⚠️ CoInitializeEx: OpenCV (DSHOW) 可能已在本线程初始化 COM (STA),
    此时再请求 MTA 会报 RPC_E_CHANGED_MODE。S_FALSE/已初始化都算成功,
    只需在真正初始化成功 (S_OK) 后 CoUninitialize。
    """
    S_OK = 0
    S_FALSE = 1
    RPC_E_CHANGED_MODE = 0x80010106
    # CoInitializeEx 返回负 HRESULT 时 ctypes 会抛 OSError (如
    # RPC_E_CHANGED_MODE: 线程已被 OpenCV/MSMF 初始化为 STA)。
    # 捕获后按"已初始化"处理，继续使用现有 COM 模式。
    try:
        hr_init = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except OSError:
        hr_init = RPC_E_CHANGED_MODE
    if hr_init not in (S_OK, S_FALSE) and hr_init != RPC_E_CHANGED_MODE:
        return []
    must_uninit = (hr_init == S_OK)
    devices = []
    try:
        sys_enum = c_void_p()
        clsid_enum = _clsid(CLSID_SystemDeviceEnum)
        iid_enum = _clsid(IID_ICreateDevEnum)
        hr = ole32.CoCreateInstance(byref(clsid_enum), None, CLSCTX_INPROC_SERVER,
                                    byref(iid_enum), byref(sys_enum))
        if hr != 0:
            return []
        try:
            cat = _clsid(CLSID_VideoInputDeviceCategory)
            mon_enum = c_void_p()
            hr = _vtbl_call(sys_enum, 3, byref(cat), byref(mon_enum), 0,
                            restype=HRESULT,
                            argtypes=[POINTER(_GUID), POINTER(c_void_p), c_ulong])
            if hr != 0 or not mon_enum:
                return []
            try:
                while True:
                    mon = c_void_p()
                    fetched = c_ulong(0)
                    hr = _vtbl_call(mon_enum, 3, 1, byref(mon), byref(fetched),
                                    restype=HRESULT,
                                    argtypes=[c_ulong, POINTER(c_void_p), POINTER(c_ulong)])
                    if hr != 0 or fetched.value == 0:
                        break
                    try:
                        path = _get_display_name(mon)
                        devices.append(('', path))
                    finally:
                        _vtbl_call(mon, 2)  # Release
            finally:
                _vtbl_call(mon_enum, 2)  # Release
        finally:
            _vtbl_call(sys_enum, 2)  # Release
    finally:
        if must_uninit:
            ole32.CoUninitialize()
    return devices


if __name__ == '__main__':
    import traceback
    try:
        devs = enum_dshow_video_devices()
        print('DirectShow video capture devices (order = OpenCV index):')
        for i, (n, p) in enumerate(devs):
            print(f'  [{i}] {n}  |  {p}')
    except Exception:
        traceback.print_exc()
