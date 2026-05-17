import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  DemoSpSidebar: [
    {
      type: 'category',
      label: 'Getting started',
      items: [
        'quickstart',
      ],
    },
    {
      type: 'category',
      label: 'Topics',
      items: [
        'architecture',
        'schema-reference',
      ],
    },
    {
      type: 'category',
      label: 'Services',
      items: [
        'services/l3vpn',
        'services/sdwan',
      ],
    },
    {
      type: 'category',
      label: 'Lab',
      items: [
        'lab/containerlab',
      ],
    },
    {
      type: 'category',
      label: 'Operations',
      items: [
        'troubleshooting',
      ],
    },
  ],
};

export default sidebars;
