import apiClient from './index';

export type AuthUser = {
  id?: number | null;
  username: string;
  role: 'admin' | 'user' | string;
};

export type AuthStatusResponse = {
  authEnabled: boolean;
  loggedIn: boolean;
  passwordSet?: boolean;
  passwordChangeable?: boolean;
  setupState: 'enabled' | 'password_retained' | 'no_password';
  currentUser?: AuthUser | null;
  registrationEnabled?: boolean;
  registrationInviteRequired?: boolean;
};

export type CaptchaResponse = {
  question: string;
  captchaToken: string;
};

export type AppUser = {
  id: number;
  username: string;
  role: 'admin' | 'user';
  status: 'active' | 'disabled';
  createdAt?: string | null;
  lastLoginAt?: string | null;
};

export type UserListResponse = {
  total: number;
  items: AppUser[];
};

export const authApi = {
  async getStatus(): Promise<AuthStatusResponse> {
    const { data } = await apiClient.get<AuthStatusResponse>('/api/v1/auth/status');
    return data;
  },

  async updateSettings(
    authEnabled: boolean,
    password?: string,
    passwordConfirm?: string,
    currentPassword?: string
  ): Promise<AuthStatusResponse> {
    const body: {
      authEnabled: boolean;
      password?: string;
      passwordConfirm?: string;
      currentPassword?: string;
    } = { authEnabled };
    if (password !== undefined) {
      body.password = password;
    }
    if (passwordConfirm !== undefined) {
      body.passwordConfirm = passwordConfirm;
    }
    if (currentPassword !== undefined) {
      body.currentPassword = currentPassword;
    }
    const { data } = await apiClient.post<AuthStatusResponse>('/api/v1/auth/settings', body);
    return data;
  },

  async login(password: string, passwordConfirm?: string, username?: string): Promise<void> {
    const body: { password: string; passwordConfirm?: string; username?: string } = { password };
    if (passwordConfirm !== undefined) {
      body.passwordConfirm = passwordConfirm;
    }
    if (username !== undefined && username.trim()) {
      body.username = username.trim();
    }
    await apiClient.post('/api/v1/auth/login', body);
  },

  async getCaptcha(): Promise<CaptchaResponse> {
    const { data } = await apiClient.get<CaptchaResponse>('/api/v1/auth/captcha');
    return data;
  },

  async register(
    username: string,
    password: string,
    passwordConfirm: string,
    captchaToken: string,
    captchaAnswer: string,
    inviteCode?: string
  ): Promise<void> {
    await apiClient.post('/api/v1/auth/register', {
      username,
      password,
      passwordConfirm,
      captchaToken,
      captchaAnswer,
      inviteCode,
    });
  },

  async changePassword(
    currentPassword: string,
    newPassword: string,
    newPasswordConfirm: string
  ): Promise<void> {
    await apiClient.post('/api/v1/auth/change-password', {
      currentPassword,
      newPassword,
      newPasswordConfirm,
    });
  },

  async listUsers(): Promise<UserListResponse> {
    const { data } = await apiClient.get<UserListResponse>('/api/v1/auth/users');
    return data;
  },

  async updateUser(
    userId: number,
    updates: { role?: 'admin' | 'user'; status?: 'active' | 'disabled'; password?: string }
  ): Promise<{ ok: boolean; user: AppUser }> {
    const { data } = await apiClient.patch<{ ok: boolean; user: AppUser }>(`/api/v1/auth/users/${userId}`, updates);
    return data;
  },

  async logout(): Promise<void> {
    await apiClient.post('/api/v1/auth/logout');
  },
};
