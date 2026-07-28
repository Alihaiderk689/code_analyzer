import { Outlet } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import MarketingNav from './MarketingNav'
import AppNav from './AppNav'

// "/" (and "/login", "/register") use this layout for anonymous visitors, but
// a logged-in user landing here (e.g. via the logo) shouldn't see the
// signed-out "Sign in / Start free" nav - show them the real app nav
// (Dashboard/New Analysis/History) instead.
export default function MarketingLayout() {
  const { user, initializing } = useAuth()

  return (
    <>
      {!initializing && user ? <AppNav /> : <MarketingNav />}
      <Outlet />
    </>
  )
}
