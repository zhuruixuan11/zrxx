/** 用户投资画像 API */
import request from './request'

export interface ProfileData {
  username: string
  risk_level: string
  investment_horizon: string
  preferred_sectors: string[]
  avoided_sectors: string[]
  investment_style: string
  bio: string
}

export interface ProfileOptions {
  risk_levels: { value: string; label: string }[]
  horizons: { value: string; label: string }[]
}

export interface ProfileResult {
  profile: ProfileData
  summary: string
  options?: ProfileOptions
}

export type ProfileUpdate = Partial<
  Pick<
    ProfileData,
    'risk_level' | 'investment_horizon' | 'preferred_sectors' | 'avoided_sectors' | 'investment_style' | 'bio'
  >
>

export const profileApi = {
  get() {
    return request.get<ProfileResult>('/api/profile')
  },
  update(payload: ProfileUpdate) {
    return request.put<ProfileResult>('/api/profile', payload)
  },
}
