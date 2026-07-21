import { Outlet } from 'react-router-dom'
import MarketingNav from './MarketingNav'

export default function MarketingLayout() {
  return (
    <>
      <MarketingNav />
      <Outlet />
    </>
  )
}
