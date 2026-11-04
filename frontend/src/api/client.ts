import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse } from 'axios';
import { message } from 'antd';
import type { ApiEnvelope } from './types';

/** Shared axios wrapper:
 *  - all REST calls use relative /api paths: vite proxies them in dev, FastAPI serves them same-origin in production
 *  - unwraps {ok,data,error}: ok=false raises antd message.error(error)
 *  - network and HTTP errors raise the same error toast
 *  - the silent option serves polling and other noisy callers so toasts do not spam
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
      return 'Operation conflicts with the current state (409)';
    }
    return `Request failed (HTTP ${axiosErr.response.status})`;
  }
  if (axiosErr.code === 'ECONNABORTED') {
    return 'Request timed out. Please check the backend service.';
  }
  return 'Cannot connect to the backend. Please check the backend address and service status.';
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
        const errText = body.error || 'Request failed';
        if (!silent) message.error(errText);
        return Promise.reject(new ApiError(errText, response.status));
      }
      // Unwrap to data so callers receive the payload directly
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

export function apiDelete<T>(url: string, config?: ApiRequestConfig): Promise<T> {
  return instance.delete<unknown, AxiosResponse<T>>(url, config).then((r) => r.data);
}

export default instance;
