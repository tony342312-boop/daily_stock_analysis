import apiClient from './index';

export type FeedbackCategory = 'bug' | 'iteration' | 'other';

export type FeedbackSubmitRequest = {
  category: FeedbackCategory;
  content: string;
  contact?: string;
  pageUrl?: string;
};

export type FeedbackSubmitResponse = {
  ok: boolean;
  notificationSent: boolean;
};

export const feedbackApi = {
  async submit(payload: FeedbackSubmitRequest): Promise<FeedbackSubmitResponse> {
    const { data } = await apiClient.post<FeedbackSubmitResponse>('/api/v1/feedback', payload);
    return data;
  },
};
