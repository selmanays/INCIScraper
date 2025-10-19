type PageRoutesType = {
  title: string;
  items: PageRoutesItemType;
};

type PageRoutesItemType = {
  title: string;
  href: string;
  icon?: string;
  isComing?: boolean;
  items?: PageRoutesItemType;
}[];

export const page_routes: PageRoutesType[] = [
  {
    title: "Genel",
    items: [
      {
        title: "CRM",
        href: "/dashboard/crm",
        icon: "BarChart3"
      },
      {
        title: "Kullanıcılar",
        href: "/dashboard/pages/users",
        icon: "Users"
      },
      {
        title: "Ayarlar",
        href: "/dashboard/pages/settings",
        icon: "Settings"
      }
    ]
  }
];
