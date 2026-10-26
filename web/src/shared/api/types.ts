export interface User {
  id: string;
  email: string;
  nickname: string;
  plan: string;
  status: string;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user?: User;
}

export interface LoginInput {
  email: string;
  password: string;
  device_name?: string;
}

export interface RegisterInput {
  email: string;
  password: string;
  nickname?: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}
