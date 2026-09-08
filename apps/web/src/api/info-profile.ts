// Profile ownership remains in the existing profile service until Core exposes
// its user profile contract. Connector operations are always real Knowledge
// API calls and are re-exported here for the Profile page.
export { getProfile, updateProfile, uploadAvatar, removeAvatar } from '@/mock-api/info-profile'
export {
	bindWechat,
	getWechatStatus,
	stopWechat,
  connectorCatalog,
  getFeishuAuthorizeURL,
  getConnectors,
  unbindConnector,
  type Connector,
  type ConnectorPlatform,
  type ConnectorDTO,
} from './info-knowledge'
export type { Profile } from '@/mock-api/info-profile'
