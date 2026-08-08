#pragma once

#include <string>

struct IPluginSelf;

namespace RuptureCompanion::LiveContext
{
bool Initialize(IPluginSelf* self);
void Shutdown();
std::string Snapshot();
} // namespace RuptureCompanion::LiveContext
