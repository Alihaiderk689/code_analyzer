import { Route, Routes } from 'react-router-dom'
import MarketingLayout from './components/MarketingLayout'
import ProtectedRoute, { AdminRoute, UserRoute, GuestRoute } from './components/ProtectedRoute'
import ConnectivityBanner from './components/ConnectivityBanner'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Register from './pages/Register'
import VerifyEmail from './pages/VerifyEmail'
import ResetPassword from './pages/ResetPassword'
import Dashboard from './pages/Dashboard'
import NewAnalysis from './pages/NewAnalysis'
import History from './pages/History'
import Report from './pages/Report'
import AnalysisChat from './pages/AnalysisChat'
import Admin from './pages/Admin'
import Settings from './pages/Settings'
import GitHub from './pages/GitHub'
import GitHubPullRequests from './pages/GitHubPullRequests'
import GitHubPullRequestDetail from './pages/GitHubPullRequestDetail'
import GitHubRepositoryFiles from './pages/GitHubRepositoryFiles'

function App() {
  return (
    <>
      <ConnectivityBanner />
      <Routes>
        <Route element={<MarketingLayout />}>
          <Route path="/" element={<Landing />} />
          <Route element={<GuestRoute />}>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
          </Route>
        </Route>

        <Route path="/verify-email" element={<VerifyEmail />} />
        <Route path="/reset-password" element={<ResetPassword />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<UserRoute />}>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/analyze" element={<NewAnalysis />} />
            <Route path="/history" element={<History />} />
            <Route path="/report/:id" element={<Report />} />
            <Route path="/report/:id/chat" element={<AnalysisChat />} />
            <Route path="/github" element={<GitHub />} />
            <Route path="/github/pull-requests" element={<GitHubPullRequests />} />
            <Route path="/github/pull-requests/:id" element={<GitHubPullRequestDetail />} />
            <Route path="/github/repositories/:pk/files" element={<GitHubRepositoryFiles />} />
          </Route>
          <Route path="/settings" element={<Settings />} />
          <Route element={<AdminRoute />}>
            <Route path="/admin" element={<Admin />} />
          </Route>
        </Route>
      </Routes>
    </>
  )
}

export default App
