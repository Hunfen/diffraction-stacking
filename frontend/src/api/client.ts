import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from 'axios';
import { message } from 'antd';
import type { ApiEnvelope } from './types';

/** 统一 axios 封装：
 *  - 所有 REST 走 /api 相对路径：dev 由 vite 代理到后端，生产由 FastAPI 托管同源
 *  - 解析 {ok,data,error}：ok=false 时 antd message.error(error)
 *  - 网络/HTTP 错误同样弹错提示
 *  - silent 配置用于轮询等场景，避免错误弹窗刷屏
 */

export interface ApiRequestConfig extends AxiosRequestConfig {
  silent?: boolean;
}

export class ApiError extends Error {
  status?: number;
  constructor(msg: string, status?: number) {
    super(msg);
    this.name = 'ApiError';
    this.status = status;
  }
}

function extractErrorMessage(err: unknown): string {
  const axiosErr = err as AxiosError<ApiEnvelope<unknown>>;
  if (axiosErr.response) {
    const body = axiosErr.response.data;
    if (body && typeof body === 'object' && body.error) {
      return body.error;
    }
    if (axiosErr.response.status === 409) {
      return '操作与当前状态冲突（409）';
    }
    return `请求失败（HTTP ${axiosErr.response.status}）`;
  }
  if (axiosErr.code === 'ECONNABORTED') {
    return '请求超时，请检查后端服务';
  }
  return '无法连接到后端服务，请检查后端地址与服务状态';
}

const instance = axios.create({
  baseURL: '/',
  timeout: 15000,
});

instance.interceptors.response.use(
  (response) => {
    const body = response.data as ApiEnvelope<unknown> | undefined;
    const silent = Boolean((response.config as ApiRequestConfig).silent);
    if (body && typeof body === 'object' && 'ok' in body) {
      if (!body.ok) {
        const errText = body.error || '请求失败';
        if (!silent) message.error(errText);
        return Promise.reject(new ApiError(errText, response.status));
      }
      // 解包成 data，调用方拿到的直接是业务数据
      return { ...response, data: body.data } as AxiosResponse;
    }
    return response;
  },
  (error) => {
    const config = (error as AxiosError).config as ApiRequestConfig | undefined;
    if (!config?.silent) {
      message.error(extractErrorMessage(error));
    }
    return Promise.reject(error);
  },
);

export function apiGet<T>(url: string, config?: ApiRequestConfig): Promise<T> {
  return instance.get<unknown, AxiosResponse<T>>(url, config).then((r) => r.data);
}

export function apiPost<T>(url: string, body?: unknown, config?: ApiRequestConfig): Promise<T> {
  return instance.post<unknown, AxiosResponse<T>>(url, body, config).then((r) => r.data);
}

export function apiPut<T>(url: string, body?: unknown, config?: ApiRequestConfig): Promise<T> {
  return instance.put<unknown, AxiosResponse<T>>(url, body, config).then((r) => r.data);
}

export default instance;
