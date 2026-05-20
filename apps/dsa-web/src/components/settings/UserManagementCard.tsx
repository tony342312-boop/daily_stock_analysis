import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { authApi, type AppUser } from '../../api/auth';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { useAuth } from '../../hooks';
import { Badge, Button, Input } from '../common';
import { SettingsAlert } from './SettingsAlert';
import { SettingsSectionCard } from './SettingsSectionCard';

export const UserManagementCard: React.FC = () => {
  const { currentUser } = useAuth();
  const isAdmin = currentUser?.role === 'admin';
  const [users, setUsers] = useState<AppUser[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [pendingUserId, setPendingUserId] = useState<number | null>(null);
  const [resetPasswords, setResetPasswords] = useState<Record<number, string>>({});
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [success, setSuccess] = useState('');

  const loadUsers = useCallback(async () => {
    if (!isAdmin) return;
    setIsLoading(true);
    setError(null);
    try {
      const data = await authApi.listUsers();
      setUsers(data.items || []);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    void loadUsers();
  }, [loadUsers]);

  if (!isAdmin) {
    return null;
  }

  const updateUser = async (user: AppUser, updates: { role?: 'admin' | 'user'; status?: 'active' | 'disabled'; password?: string }) => {
    setPendingUserId(user.id);
    setError(null);
    setSuccess('');
    try {
      const result = await authApi.updateUser(user.id, updates);
      setUsers((prev) => prev.map((item) => item.id === user.id ? result.user : item));
      setSuccess(`账号 ${user.username} 已更新`);
      setResetPasswords((prev) => ({ ...prev, [user.id]: '' }));
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setPendingUserId(null);
    }
  };

  return (
    <SettingsSectionCard
      title="用户管理"
      description="管理员可查看账号、停用/启用普通用户、调整角色或重置密码。"
      actions={<Button type="button" variant="settings-secondary" size="sm" onClick={() => void loadUsers()} disabled={isLoading}>刷新</Button>}
    >
      <div className="space-y-3">
        {error ? <SettingsAlert title="用户管理失败" message={error.message} variant="error" /> : null}
        {success ? <SettingsAlert title="操作成功" message={success} variant="success" /> : null}
        {isLoading ? <p className="text-sm text-muted-text">正在加载用户...</p> : null}
        <div className="space-y-2">
          {users.map((user) => {
            const busy = pendingUserId === user.id;
            const isSelf = user.id === currentUser?.id;
            const password = resetPasswords[user.id] || '';
            return (
              <div key={user.id} className="rounded-2xl border settings-border bg-background/40 px-4 py-3">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-foreground">{user.username}</span>
                      <Badge variant={user.role === 'admin' ? 'warning' : 'default'} size="sm">{user.role === 'admin' ? '管理员' : '普通用户'}</Badge>
                      <Badge variant={user.status === 'active' ? 'success' : 'danger'} size="sm">{user.status === 'active' ? '启用' : '停用'}</Badge>
                      {isSelf ? <Badge variant="info" size="sm">当前账号</Badge> : null}
                    </div>
                    <p className="mt-1 text-xs text-muted-text">创建：{user.createdAt || '-'}；最后登录：{user.lastLoginAt || '-'}</p>
                  </div>
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                    <Input
                      label="重置密码"
                      type="password"
                      value={password}
                      onChange={(event) => setResetPasswords((prev) => ({ ...prev, [user.id]: event.target.value }))}
                      placeholder="6-16位，非纯数字"
                      disabled={busy}
                    />
                    <Button type="button" variant="settings-secondary" disabled={busy || !password} onClick={() => void updateUser(user, { password })}>重置</Button>
                    <Button type="button" variant="settings-secondary" disabled={busy || isSelf} onClick={() => void updateUser(user, { status: user.status === 'active' ? 'disabled' : 'active' })}>
                      {user.status === 'active' ? '停用' : '启用'}
                    </Button>
                    <Button type="button" variant="settings-secondary" disabled={busy || isSelf} onClick={() => void updateUser(user, { role: user.role === 'admin' ? 'user' : 'admin' })}>
                      设为{user.role === 'admin' ? '普通用户' : '管理员'}
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </SettingsSectionCard>
  );
};
