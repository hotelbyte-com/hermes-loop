// `pnpm seed` — create/migrate the store and seed the PM scenario.

import { createStore } from '../store.ts'
import { seedPmScenario } from '../seed/pm-scenario.ts'

const db = createStore()
const result = seedPmScenario(db)
db.close()

console.log('[loop] seed complete')
console.log('  workspaceId :', result.workspaceId)
console.log('  channelId    :', result.channelId)
console.log('  aliceId      :', result.aliceId)
console.log('  machineId    :', result.machineId, '(token written to .data/machine.json)')
console.log('  bots         :', Object.keys(result.botAgentIds).join(', '))
console.log('  demoMessages :', result.demoMessages)
