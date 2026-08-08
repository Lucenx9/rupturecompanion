#include "live_context.h"

#include "plugin_interface.h"

#include "AuCrafting_structs.hpp"
#include "AuItems_classes.hpp"
#include "Chimera_classes.hpp"
#include "Chimera_structs.hpp"
#include "Engine_classes.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <locale>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace RuptureCompanion::LiveContext
{
namespace
{
using Fields = std::vector<std::pair<std::string, std::string>>;

constexpr float SampleIntervalSeconds = 0.75f;
constexpr int MaxInventorySlots = 256;
constexpr int MaxInventoryItems = 128;
constexpr int MaxObjectives = 32;
constexpr int MaxSubObjectives = 64;
constexpr int MaxInteractedItems = 32;
constexpr int MaxTechnologyEntries = 64;
constexpr std::size_t MaxStringBytes = 128;
constexpr std::size_t MaxSnapshotBytes = 48 * 1024;

IPluginSelf* g_self = nullptr;
SDK::UWorld* g_world = nullptr;
float g_sampleAccumulator = SampleIntervalSeconds;
std::mutex g_snapshotMutex;
std::string g_snapshot;
bool g_registered = false;

std::string TruncateUtf8(std::string value, const std::size_t limit = MaxStringBytes)
{
    if (value.size() <= limit)
    {
        return value;
    }
    std::size_t end = limit;
    while (end > 0
           && (static_cast<unsigned char>(value[end]) & 0xC0U) == 0x80U)
    {
        --end;
    }
    value.resize(end);
    return value;
}

std::string JsonString(const std::string_view value)
{
    std::string output;
    output.reserve(value.size() + 2);
    output.push_back('"');
    constexpr char Hex[] = "0123456789abcdef";
    for (const unsigned char character : value)
    {
        switch (character)
        {
        case '"':
            output += "\\\"";
            break;
        case '\\':
            output += "\\\\";
            break;
        case '\b':
            output += "\\b";
            break;
        case '\f':
            output += "\\f";
            break;
        case '\n':
            output += "\\n";
            break;
        case '\r':
            output += "\\r";
            break;
        case '\t':
            output += "\\t";
            break;
        default:
            if (character < 0x20U)
            {
                output += "\\u00";
                output.push_back(Hex[(character >> 4U) & 0x0FU]);
                output.push_back(Hex[character & 0x0FU]);
            }
            else
            {
                output.push_back(static_cast<char>(character));
            }
        }
    }
    output.push_back('"');
    return output;
}

std::string JsonNumber(const double value)
{
    if (!std::isfinite(value))
    {
        return "null";
    }
    std::ostringstream output;
    output.imbue(std::locale::classic());
    output << std::fixed << std::setprecision(3) << value;
    std::string result = output.str();
    while (result.size() > 1 && result.back() == '0')
    {
        result.pop_back();
    }
    if (!result.empty() && result.back() == '.')
    {
        result.pop_back();
    }
    return result == "-0" ? "0" : result;
}

std::string JsonInteger(const std::int64_t value)
{
    return std::to_string(value);
}

std::string JsonBoolean(const bool value)
{
    return value ? "true" : "false";
}

std::string JsonObject(const Fields& fields)
{
    std::string output = "{";
    bool first = true;
    for (const auto& [key, value] : fields)
    {
        if (!first)
        {
            output.push_back(',');
        }
        first = false;
        output += JsonString(key);
        output.push_back(':');
        output += value;
    }
    output.push_back('}');
    return output;
}

std::string JsonArray(const std::vector<std::string>& values)
{
    std::string output = "[";
    bool first = true;
    for (const std::string& value : values)
    {
        if (!first)
        {
            output.push_back(',');
        }
        first = false;
        output += value;
    }
    output.push_back(']');
    return output;
}

void Add(Fields& fields, const std::string_view key, std::string value)
{
    fields.emplace_back(std::string(key), std::move(value));
}

void AddNumber(Fields& fields, const std::string_view key, const double value)
{
    if (std::isfinite(value))
    {
        Add(fields, key, JsonNumber(value));
    }
}

std::int64_t UnixTimeMilliseconds()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string SafeText(const SDK::FText& text)
{
    return text.TextData == nullptr ? std::string{} : TruncateUtf8(text.ToString());
}

std::string ItemName(const SDK::UAuItemDataBase* item)
{
    if (item == nullptr)
    {
        return {};
    }
    std::string name = SafeText(item->ItemName);
    if (name.empty())
    {
        name = TruncateUtf8(item->UniqueItemName.ToString());
    }
    if (name.empty())
    {
        name = TruncateUtf8(item->GetName());
    }
    return name;
}

std::string ObjectName(const SDK::UObject* object)
{
    return object == nullptr ? std::string{} : TruncateUtf8(object->GetName());
}

std::string ClassName(const SDK::UObject* object)
{
    return object == nullptr || object->Class == nullptr
        ? std::string{}
        : TruncateUtf8(object->Class->GetName());
}

bool ValidGuid(const SDK::FGuid& guid)
{
    return guid.A != 0 || guid.B != 0 || guid.C != 0 || guid.D != 0;
}

bool SameGuid(const SDK::FGuid& left, const SDK::FGuid& right)
{
    return left.A == right.A && left.B == right.B && left.C == right.C
        && left.D == right.D;
}

std::string GuidString(const SDK::FGuid& guid)
{
    std::ostringstream output;
    output << std::hex << std::setfill('0') << std::setw(8)
           << static_cast<std::uint32_t>(guid.A) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.B) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.C) << '-' << std::setw(8)
           << static_cast<std::uint32_t>(guid.D);
    return output.str();
}

const char* ObjectiveStateName(const SDK::ECrObjectiveState state)
{
    switch (state)
    {
    case SDK::ECrObjectiveState::Inactive:
        return "inactive";
    case SDK::ECrObjectiveState::Active:
        return "active";
    case SDK::ECrObjectiveState::Completed:
        return "completed";
    default:
        return "unknown";
    }
}

const char* WaveTypeName(const SDK::EEnviroWave type)
{
    switch (type)
    {
    case SDK::EEnviroWave::None:
        return "none";
    case SDK::EEnviroWave::Heat:
        return "heat";
    case SDK::EEnviroWave::Cold:
        return "cold";
    default:
        return "unknown";
    }
}

const char* WaveStageName(const SDK::EEnviroWaveStage stage)
{
    switch (stage)
    {
    case SDK::EEnviroWaveStage::None:
        return "none";
    case SDK::EEnviroWaveStage::PreWave:
        return "pre_wave";
    case SDK::EEnviroWaveStage::Moving:
        return "moving";
    case SDK::EEnviroWaveStage::Fadeout:
        return "fadeout";
    case SDK::EEnviroWaveStage::Growback:
        return "growback";
    default:
        return "unknown";
    }
}

const char* BuildingStateName(const SDK::ECrBuildingState state)
{
    switch (state)
    {
    case SDK::ECrBuildingState::Unknown:
        return "unknown";
    case SDK::ECrBuildingState::NoElectricity:
        return "no_electricity";
    case SDK::ECrBuildingState::Working:
        return "working";
    case SDK::ECrBuildingState::Inefficient:
        return "inefficient";
    case SDK::ECrBuildingState::Blocked:
        return "blocked";
    case SDK::ECrBuildingState::NotWorking:
        return "not_working";
    default:
        return "unknown";
    }
}

const char* GridConnectionName(const SDK::ECrBuildingGridConnectionStatus status)
{
    switch (status)
    {
    case SDK::ECrBuildingGridConnectionStatus::NotConnected:
        return "not_connected";
    case SDK::ECrBuildingGridConnectionStatus::Connected:
        return "connected";
    case SDK::ECrBuildingGridConnectionStatus::ConnectedAndOff:
        return "connected_off";
    default:
        return "unknown";
    }
}

const char* AlienActivityName(const SDK::EHUDAlienActivityType type)
{
    switch (type)
    {
    case SDK::EHUDAlienActivityType::AlienSignaturesDetected:
        return "alien_signatures_detected";
    case SDK::EHUDAlienActivityType::NoHostileActivity:
        return "no_hostile_activity";
    case SDK::EHUDAlienActivityType::SignalLost:
        return "signal_lost";
    case SDK::EHUDAlienActivityType::None:
        return "none";
    default:
        return "unknown";
    }
}

const char* SkillName(const SDK::ECrPlayerProgressionSkill skill)
{
    switch (skill)
    {
    case SDK::ECrPlayerProgressionSkill::Movement:
        return "movement";
    case SDK::ECrPlayerProgressionSkill::Combat:
        return "combat";
    case SDK::ECrPlayerProgressionSkill::Survival:
        return "survival";
    case SDK::ECrPlayerProgressionSkill::None:
        return "none";
    default:
        return "unknown";
    }
}

void AddUnlockedFeature(
    std::vector<std::string>& features,
    const SDK::ECrCorporationUnlockedFeatures flags,
    const SDK::ECrCorporationUnlockedFeatures feature,
    const std::string_view name)
{
    const auto rawFlags = static_cast<std::uint8_t>(flags);
    const auto rawFeature = static_cast<std::uint8_t>(feature);
    if ((rawFlags & rawFeature) != 0U)
    {
        features.push_back(JsonString(name));
    }
}

std::string AttributeJson(const SDK::FCrPlayerAttributeSaveData& attribute)
{
    Fields fields;
    AddNumber(fields, "current", attribute.Current);
    AddNumber(fields, "min", attribute.Min);
    AddNumber(fields, "max", attribute.Max);
    return JsonObject(fields);
}

std::string BuildVitals(const SDK::ACrCharacterPlayerBase* player)
{
    const SDK::FCrCharacterPlayerSurvivalData data = player->GetCharacterSurvivalData();
    Fields fields;
    Add(fields, "health", AttributeJson(data.Health));
    Add(fields, "energy", AttributeJson(data.Energy));
    Add(fields, "shield", AttributeJson(data.Shield));
    Add(fields, "oxygen", AttributeJson(data.Oxygen));
    Add(fields, "hydration", AttributeJson(data.Hydration));
    Add(fields, "calories", AttributeJson(data.Calories));
    Add(fields, "toxicity", AttributeJson(data.Toxicity));
    Add(fields, "radiation", AttributeJson(data.Radiation));
    Add(fields, "heat", AttributeJson(data.Heat));
    Add(fields, "drain", AttributeJson(data.Drain));
    Add(fields, "corrosion", AttributeJson(data.Corrosion));
    Add(fields, "infection", AttributeJson(data.Infection));
    Add(fields, "med_tool_charge", AttributeJson(data.MedToolCharge));
    Add(fields, "grenade_charge", AttributeJson(data.GrenadeCharge));
    Add(fields, "movement_speed_multiplier", AttributeJson(data.MovementSpeedMultiplier));
    AddNumber(fields, "temperature", player->GetCurrentTemperature());
    return JsonObject(fields);
}

std::string BuildInventory(
    SDK::ACrCharacterPlayerBase* player,
    bool& truncated,
    bool& available)
{
    SDK::UCrInventoryComponent* inventory = player->BP_GetInventory();
    SDK::UCrInventoryItemsStoreComponent* store = player->InventoryItemsStore;
    if (inventory == nullptr || store == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    const int slotCount = inventory->Slots.Num();
    const int safeSlotCount = std::clamp(slotCount, 0, MaxInventorySlots);
    truncated = slotCount > MaxInventorySlots;
    std::map<std::string, std::int64_t> itemAmounts;
    std::set<std::string> seenItemIds;
    int occupiedSlots = 0;
    for (int index = 0; index < safeSlotCount; ++index)
    {
        if (!inventory->Slots.IsValidIndex(index))
        {
            continue;
        }
        const SDK::FCrInventorySlot& slot = inventory->Slots[index];
        if (!ValidGuid(slot.ItemId.Handle))
        {
            continue;
        }
        if (!seenItemIds.insert(GuidString(slot.ItemId.Handle)).second)
        {
            continue;
        }
        const SDK::FAuItemEntry entry = store->GetItemCopy(slot.ItemId);
        if (entry.Amount <= 0 || entry.ItemDataInstance == nullptr)
        {
            continue;
        }
        std::string name = ItemName(entry.ItemDataInstance);
        if (name.empty())
        {
            continue;
        }
        ++occupiedSlots;
        itemAmounts[std::move(name)] += entry.Amount;
    }

    std::vector<std::string> items;
    for (const auto& [name, amount] : itemAmounts)
    {
        if (items.size() >= MaxInventoryItems)
        {
            truncated = true;
            break;
        }
        Fields itemFields;
        Add(itemFields, "name", JsonString(name));
        Add(itemFields, "amount", JsonInteger(amount));
        items.push_back(JsonObject(itemFields));
    }
    Fields fields;
    Add(fields, "grid_columns", JsonInteger(inventory->GridColumns));
    Add(fields, "grid_rows", JsonInteger(inventory->GridRows));
    Add(fields, "capacity_cells", JsonInteger(
            std::max(0, inventory->GridColumns) * std::max(0, inventory->GridRows)));
    Add(fields, "occupied_slots", JsonInteger(occupiedSlots));
    Add(fields, "items", JsonArray(items));
    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildEquipment(SDK::ACrCharacterPlayerBase* player)
{
    Fields fields;
    const SDK::UCrWeaponItemDataBase* weapon = player->GetEquippedWeaponItemDataBase();
    if (weapon != nullptr)
    {
        Add(fields, "weapon", JsonString(ItemName(weapon)));
    }
    SDK::UCrWeaponComponent* component = player->BP_GetWeaponComponent();
    if (component != nullptr && weapon != nullptr)
    {
        AddNumber(fields, "ammo_loaded", component->GetEquippedWeaponCurrentAmmo());
        AddNumber(fields, "ammo_capacity", component->GetEquippedWeaponMaxAmmo());
        AddNumber(fields, "ammo_in_inventory", component->GetEquippedWeaponAmmoInInventory());
        Add(fields, "firing", JsonBoolean(component->IsFireInputDown()));
    }
    return JsonObject(fields);
}

std::vector<std::string> TechnologyNames(
    const SDK::TArray<SDK::UCrBuildingData*>& entries,
    bool& truncated)
{
    std::vector<std::string> names;
    std::set<std::string> seen;
    const int count = std::min(entries.Num(), MaxTechnologyEntries);
    truncated = truncated || entries.Num() > MaxTechnologyEntries;
    for (int index = 0; index < count; ++index)
    {
        if (!entries.IsValidIndex(index) || entries[index] == nullptr)
        {
            continue;
        }
        std::string name = SafeText(entries[index]->BuildingName);
        if (name.empty())
        {
            name = ObjectName(entries[index]);
        }
        if (!name.empty() && seen.insert(name).second)
        {
            names.push_back(JsonString(name));
        }
    }
    return names;
}

std::vector<std::string> TechnologyNames(
    const SDK::TArray<SDK::UCrItemRecipeData*>& entries,
    bool& truncated)
{
    std::vector<std::string> names;
    std::set<std::string> seen;
    const int count = std::min(entries.Num(), MaxTechnologyEntries);
    truncated = truncated || entries.Num() > MaxTechnologyEntries;
    for (int index = 0; index < count; ++index)
    {
        if (!entries.IsValidIndex(index) || entries[index] == nullptr)
        {
            continue;
        }
        std::string name = SafeText(entries[index]->DisplayText);
        if (name.empty())
        {
            name = ObjectName(entries[index]);
        }
        if (!name.empty() && seen.insert(name).second)
        {
            names.push_back(JsonString(name));
        }
    }
    return names;
}

std::string BuildProgression(
    SDK::ACrGameStateBase* gameState,
    SDK::ACrPlayerControllerBase* controller,
    bool& available,
    bool& truncated)
{
    if (gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    AddNumber(fields, "playtime_seconds", gameState->PlaytimeDuration);
    Add(fields, "tutorial_completed", JsonBoolean(gameState->bTutorialCompleted));
    Add(fields, "in_tutorial", JsonBoolean(gameState->IsInTutorial()));

    if (controller != nullptr)
    {
        std::vector<std::string> skills;
        const int skillCount = std::min(controller->PlayerSkills.Num(), 16);
        truncated = truncated || controller->PlayerSkills.Num() > 16;
        for (int index = 0; index < skillCount; ++index)
        {
            if (!controller->PlayerSkills.IsValidIndex(index))
            {
                continue;
            }
            const SDK::FCrSkillData& skill = controller->PlayerSkills[index];
            Fields skillFields;
            Add(skillFields, "name", JsonString(SkillName(skill.Skill)));
            Add(skillFields, "level", JsonInteger(skill.Level));
            AddNumber(skillFields, "experience", skill.Experience);
            skills.push_back(JsonObject(skillFields));
        }
        Add(fields, "skills", JsonArray(skills));
    }

    SDK::ACrCorporationsOwner* corporations = gameState->CorporationsOwner;
    if (corporations != nullptr)
    {
        Add(fields, "data_points", JsonInteger(corporations->DataPoints));
        Add(fields, "unlocked_inventory_slots", JsonInteger(
                corporations->GetUnlockedInventorySlotsNumber()));
        std::vector<std::string> unlockedFeatures;
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Map, "map");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Grenade, "grenade");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::MedTool, "med_tool");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::BuildingDrone, "building_drone");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Pistol, "pistol");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::BiggerInventory, "bigger_inventory");
        AddUnlockedFeature(unlockedFeatures, corporations->UnlockedFeaturesFlags,
            SDK::ECrCorporationUnlockedFeatures::Dash, "dash");
        Add(fields, "unlocked_features", JsonArray(unlockedFeatures));

        std::vector<std::string> corporationEntries;
        const auto& corporationData = corporations->CorporationsContainer.CorporationsData;
        const int corporationCount = std::min(corporationData.Num(), 16);
        truncated = truncated || corporationData.Num() > 16;
        for (int index = 0; index < corporationCount; ++index)
        {
            if (!corporationData.IsValidIndex(index) || corporationData[index].bHidden)
            {
                continue;
            }
            const SDK::FCrCorporation& corporation = corporationData[index];
            Fields corporationFields;
            Add(corporationFields, "name", JsonString(
                    TruncateUtf8(corporation.Name.ToString())));
            Add(corporationFields, "level", JsonInteger(corporation.Level));
            Add(corporationFields, "reputation", JsonInteger(corporation.Reputation));
            Add(corporationFields, "research_points_tier_1", JsonInteger(
                    corporation.ResearchPointsTier1));
            Add(corporationFields, "research_points_tier_2", JsonInteger(
                    corporation.ResearchPointsTier2));
            corporationEntries.push_back(JsonObject(corporationFields));
        }
        Add(fields, "corporations", JsonArray(corporationEntries));
    }

    SDK::ACrTechnologyKeeper* technology = gameState->TechnologyKeeper;
    if (technology != nullptr)
    {
        Fields technologyFields;
        Add(technologyFields, "available_buildings_count", JsonInteger(
                std::max(0, technology->AvailableBuildings.Num())));
        Add(technologyFields, "replicated_recipes_count", JsonInteger(
                std::max(0, technology->AllRecipes.Num())));
        Add(technologyFields, "available_buildings", JsonArray(
                TechnologyNames(technology->AvailableBuildings, truncated)));
        Add(technologyFields, "replicated_recipes", JsonArray(
                TechnologyNames(technology->AllRecipes, truncated)));
        Add(fields, "technology", JsonObject(technologyFields));
    }

    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildSession(SDK::ACrGameStateBase* gameState, bool& available)
{
    if (gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    Add(fields, "player_count", JsonInteger(std::max(0, gameState->PlayerArray.Num())));
    Add(fields, "match_started", JsonBoolean(gameState->HasMatchStarted()));
    Add(fields, "match_ended", JsonBoolean(gameState->HasMatchEnded()));
    Add(fields, "paused", JsonBoolean(gameState->bIsGamePaused));
    Add(fields, "cutscene_active", JsonBoolean(gameState->IsCutsceneActive()));
    AddNumber(fields, "server_world_time_seconds", gameState->GetServerWorldTimeSeconds());
    return JsonObject(fields);
}

const SDK::FCrObjectiveData* FindObjectiveDefinition(const SDK::FGuid& id)
{
    SDK::UCrObjectivesDeveloperSettings* settings =
        SDK::UCrObjectivesDeveloperSettings::GetDefaultObj();
    SDK::UDataTable* table = settings == nullptr
        ? nullptr
        : settings->ObjectiveEntriesDefinition.Get();
    if (table == nullptr || !table->RowMap.IsValid() || table->RowMap.Num() > 1024)
    {
        return nullptr;
    }
    for (int index = 0; index < table->RowMap.Max(); ++index)
    {
        if (!table->RowMap.IsValidIndex(index))
        {
            continue;
        }
        const std::uint8_t* row = table->RowMap[index].Value();
        if (row == nullptr)
        {
            continue;
        }
        const auto* objective = reinterpret_cast<const SDK::FCrObjectiveData*>(row);
        if (SameGuid(objective->EntryID, id))
        {
            return objective;
        }
    }
    return nullptr;
}

std::string BuildObjectives(SDK::ACrGameStateBase* gameState, bool& truncated, bool& available)
{
    SDK::ACrObjectivesOwner* owner = gameState == nullptr ? nullptr : gameState->ObjectivesOwner;
    if (owner == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    const auto& entries = owner->ObjectivesEntriesContainer.EntriesStatusData;
    const int entryCount = entries.Num();
    const int safeEntryCount = std::clamp(entryCount, 0, 512);
    truncated = entryCount > safeEntryCount;
    int activeCount = 0;
    int completedCount = 0;
    int remainingSubObjectives = MaxSubObjectives;
    std::vector<std::string> active;
    for (int index = 0; index < safeEntryCount; ++index)
    {
        if (!entries.IsValidIndex(index))
        {
            continue;
        }
        const SDK::FCrObjectiveEntryStatus& status = entries[index];
        if (status.ObjectiveState == SDK::ECrObjectiveState::Completed)
        {
            ++completedCount;
        }
        if (status.ObjectiveState != SDK::ECrObjectiveState::Active)
        {
            continue;
        }
        ++activeCount;
        if (active.size() >= MaxObjectives)
        {
            truncated = true;
            continue;
        }
        Fields objectiveFields;
        Add(objectiveFields, "id", JsonString(GuidString(status.EntryID)));
        Add(objectiveFields, "state", JsonString(ObjectiveStateName(status.ObjectiveState)));
        const SDK::FCrObjectiveData* definition = FindObjectiveDefinition(status.EntryID);
        if (definition != nullptr)
        {
            const std::string title = SafeText(definition->EntryText);
            if (!title.empty())
            {
                Add(objectiveFields, "title", JsonString(title));
            }
        }
        std::vector<std::string> subObjectives;
        const int subCount = std::min(
            status.SubObjectives.Num(), remainingSubObjectives);
        if (status.SubObjectives.Num() > remainingSubObjectives)
        {
            truncated = true;
        }
        for (int subIndex = 0; subIndex < subCount; ++subIndex)
        {
            if (!status.SubObjectives.IsValidIndex(subIndex))
            {
                continue;
            }
            const SDK::FCrSubObjectiveEntryStatus& subStatus = status.SubObjectives[subIndex];
            Fields subFields;
            if (definition != nullptr && definition->SubObjectives.IsValidIndex(subIndex))
            {
                const std::string title = SafeText(definition->SubObjectives[subIndex].EntryText);
                if (!title.empty())
                {
                    Add(subFields, "title", JsonString(title));
                }
            }
            Add(subFields, "current", JsonInteger(subStatus.CurrentValue));
            Add(subFields, "required", JsonInteger(subStatus.ConditionValue));
            Add(subFields, "show_counter", JsonBoolean(subStatus.bShowCounter));
            subObjectives.push_back(JsonObject(subFields));
        }
        remainingSubObjectives -= subCount;
        Add(objectiveFields, "sub_objectives", JsonArray(subObjectives));
        active.push_back(JsonObject(objectiveFields));
    }
    Fields fields;
    Add(fields, "active_count", JsonInteger(activeCount));
    Add(fields, "completed_count", JsonInteger(completedCount));
    Add(fields, "active", JsonArray(active));
    Add(fields, "truncated", JsonBoolean(truncated));
    return JsonObject(fields);
}

std::string BuildEnvironment(SDK::UWorld* world, SDK::ACrGameStateBase* gameState, bool& available)
{
    auto* subsystem = static_cast<SDK::UCrEnviroWaveSubsystem*>(
        SDK::USubsystemBlueprintLibrary::GetWorldSubsystem(
            world, SDK::UCrEnviroWaveSubsystem::StaticClass()));
    if (subsystem == nullptr && gameState == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    if (subsystem != nullptr)
    {
        Add(fields, "wave_type", JsonString(WaveTypeName(subsystem->GetCurrentType())));
        Add(fields, "wave_stage", JsonString(WaveStageName(subsystem->GetCurrentStage())));
        Add(fields, "wave_in_progress", JsonBoolean(subsystem->IsWaveInProgress()));
        Add(fields, "wave_paused", JsonBoolean(subsystem->IsWavePaused()));
        AddNumber(fields, "stage_progress", subsystem->GetCurrentStageProgress());
        AddNumber(fields, "seconds_since_wave_started", subsystem->GetTimeSinceLastWaveStarted());
    }
    if (gameState != nullptr)
    {
        Add(fields, "sulfur_active", JsonBoolean(gameState->bSulphurActive));
        if (gameState->WaveTimerActor != nullptr)
        {
            AddNumber(fields, "next_wave_time", gameState->WaveTimerActor->NextTime);
            Add(fields, "next_wave_phase", JsonInteger(gameState->WaveTimerActor->NextPhase));
            Add(fields, "wave_timer_paused", JsonBoolean(gameState->WaveTimerActor->bPause));
        }
    }
    return JsonObject(fields);
}

std::string BuildTarget(
    SDK::UWorld* world,
    SDK::ACrCharacterPlayerBase* player,
    bool& available,
    bool& truncated)
{
    SDK::APlayerController* baseController = SDK::UGameplayStatics::GetPlayerController(world, 0);
    SDK::ACrPlayerControllerBase* controller =
        baseController != nullptr && baseController->IsA(SDK::ACrPlayerControllerBase::StaticClass())
        ? static_cast<SDK::ACrPlayerControllerBase*>(baseController)
        : nullptr;
    SDK::AActor* target = controller == nullptr ? nullptr : controller->GetCurrentInteractable();
    if (target == nullptr && controller != nullptr)
    {
        target = controller->CurrentInteractableActorWithActiveInteraction;
    }
    if (target == nullptr && controller != nullptr)
    {
        target = controller->CurrentBuildingTarget;
    }
    if (target == nullptr)
    {
        target = player->LookingBuilding;
    }
    if (target == nullptr)
    {
        target = player->CurrentAimingTargetActor;
    }
    if (target == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    Add(fields, "name", JsonString(ObjectName(target)));
    Add(fields, "class", JsonString(ClassName(target)));
    const SDK::FVector playerLocation = player->K2_GetActorLocation();
    const SDK::FVector targetLocation = target->K2_GetActorLocation();
    AddNumber(fields, "distance_m", playerLocation.GetDistanceToInMeters(targetLocation));

    if (target->IsA(SDK::ACrBuildingActorBase::StaticClass()))
    {
        auto* building = static_cast<SDK::ACrBuildingActorBase*>(target);
        Fields buildingFields;
        if (building->PlacementData != nullptr
            && building->PlacementData->IsA(SDK::UCrBuildingData::StaticClass()))
        {
            const auto* buildingData = static_cast<SDK::UCrBuildingData*>(
                building->PlacementData);
            const std::string displayName = SafeText(buildingData->BuildingName);
            if (!displayName.empty())
            {
                Add(fields, "display_name", JsonString(displayName));
            }
        }
        Add(buildingFields, "state", JsonString(BuildingStateName(building->GetBuildingState())));
        Add(buildingFields, "turned_on", JsonBoolean(building->IsBuildingTurnedOn()));
        Add(buildingFields, "disabled", JsonBoolean(building->IsBuildingDisabled()));
        Add(buildingFields, "infected", JsonBoolean(building->IsBuildingInfectionActive()));
        AddNumber(buildingFields, "infection", building->GetCurrentInfection());
        AddNumber(buildingFields, "temperature", building->GetCurrentTemperature());
        Add(buildingFields, "grid_connection", JsonString(
                GridConnectionName(building->GetGridConnectionStatus())));
        AddNumber(buildingFields, "building_power", building->GetBuildingPower());
        AddNumber(buildingFields, "building_potential_power", building->GetBuildingPotentialPower());
        AddNumber(buildingFields, "grid_power", building->GetGridPower());
        AddNumber(buildingFields, "grid_add_power", building->GetGridAddPower());
        AddNumber(buildingFields, "grid_remove_power", building->GetGridRemovePower());
        if (target->IsA(SDK::ACrCrafter::StaticClass()))
        {
            auto* crafter = static_cast<SDK::ACrCrafter*>(target);
            AddNumber(buildingFields, "craft_progress", crafter->GetItemCraftingProgress());
            if (crafter->CraftComponent != nullptr)
            {
                Add(buildingFields, "craft_queue_size", JsonInteger(
                        std::max(0, crafter->CraftComponent->ItemsToCraft.Num())));
                if (crafter->CraftComponent->ItemsToCraft.IsValidIndex(0))
                {
                    const SDK::FAuCraftItem& queued = crafter->CraftComponent->ItemsToCraft[0];
                    const std::string outputName = ItemName(queued.OutputItem.ItemDataBase);
                    if (!outputName.empty())
                    {
                        Add(buildingFields, "crafting_item", JsonString(outputName));
                        Add(buildingFields, "crafting_amount", JsonInteger(queued.OutputItem.Count));
                    }
                }
            }
        }
        Add(fields, "building", JsonObject(buildingFields));
    }

    if (controller != nullptr)
    {
        std::vector<std::string> interactedItems;
        const int count = std::min(
            controller->ItemListForInteractedBuilding.Num(), MaxInteractedItems);
        truncated = controller->ItemListForInteractedBuilding.Num() > MaxInteractedItems;
        for (int index = 0; index < count; ++index)
        {
            if (!controller->ItemListForInteractedBuilding.IsValidIndex(index))
            {
                continue;
            }
            const std::string name = ItemName(controller->ItemListForInteractedBuilding[index]);
            if (!name.empty())
            {
                interactedItems.push_back(JsonString(name));
            }
        }
        if (!interactedItems.empty())
        {
            Add(fields, "interacted_item_types", JsonArray(interactedItems));
        }
    }
    return JsonObject(fields);
}

std::string BuildBase(SDK::ACrGameStateBase* gameState, bool& available)
{
    SDK::ACrBaseCoreReplicationHelper* helper = gameState == nullptr
        ? nullptr
        : gameState->BaseCoreReplicationHelper;
    SDK::ACrMapMenuDataReplicationHelper* map = gameState == nullptr
        ? nullptr
        : gameState->MapMenuDataReplicationHelper;
    if (helper == nullptr && map == nullptr)
    {
        available = false;
        return "null";
    }
    available = true;
    Fields fields;
    if (helper != nullptr)
    {
        Add(fields, "alien_activity", JsonString(AlienActivityName(helper->HUDAlienActivityType)));
        AddNumber(fields, "core_integrity", helper->HUDAlienActivityCoreIntegrity);
        AddNumber(fields, "alien_signature", helper->HUDAlienActivityAlienSignature);
    }
    if (map != nullptr)
    {
        Add(fields, "map_radiation_level", JsonInteger(map->CurrentRadiationLevelReplicated));
        Add(fields, "active_base_attack_markers", JsonInteger(
                std::max(0, map->BaseAttackMarkerDataContainer.BaseAttackMarkerData.Num())));
        Add(fields, "known_points_of_interest", JsonInteger(
                std::max(0, map->PointOfInterestReplicationData.Num())));
    }
    return JsonObject(fields);
}

std::string BuildUnavailableSnapshot(const std::string_view reason)
{
    std::vector<std::string> missing = {
        JsonString("player"), JsonString("inventory"), JsonString("equipment"),
        JsonString("session"), JsonString("progression"), JsonString("objectives"),
        JsonString("environment"), JsonString("target"), JsonString("base")};
    Fields status;
    Add(status, "available", "false");
    Add(status, "partial", "true");
    Add(status, "reason", JsonString(reason));
    Add(status, "missing_sections", JsonArray(missing));
    Add(status, "truncated_sections", "[]");
    Fields root;
    Add(root, "schema_version", "1");
    Add(root, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
    Fields source;
    Add(source, "kind", JsonString("client_observed"));
    Add(source, "game_sdk_build", JsonString("CL121391"));
    Add(source, "sample_interval_ms", JsonInteger(750));
    Add(root, "source", JsonObject(source));
    Add(root, "status", JsonObject(status));
    return JsonObject(root);
}

std::string Capture(SDK::UWorld* world)
{
    if (world == nullptr)
    {
        return BuildUnavailableSnapshot("world_unavailable");
    }
    SDK::APawn* pawn = SDK::UGameplayStatics::GetPlayerPawn(world, 0);
    if (pawn == nullptr || !pawn->IsA(SDK::ACrCharacterPlayerBase::StaticClass()))
    {
        return BuildUnavailableSnapshot("local_player_unavailable");
    }
    auto* player = static_cast<SDK::ACrCharacterPlayerBase*>(pawn);
    SDK::AGameStateBase* baseGameState = SDK::UGameplayStatics::GetGameState(world);
    SDK::ACrGameStateBase* gameState =
        baseGameState != nullptr && baseGameState->IsA(SDK::ACrGameStateBase::StaticClass())
        ? static_cast<SDK::ACrGameStateBase*>(baseGameState)
        : nullptr;
    SDK::APlayerController* baseController = SDK::UGameplayStatics::GetPlayerController(world, 0);
    SDK::ACrPlayerControllerBase* controller =
        baseController != nullptr && baseController->IsA(SDK::ACrPlayerControllerBase::StaticClass())
        ? static_cast<SDK::ACrPlayerControllerBase*>(baseController)
        : nullptr;

    std::vector<std::string> missing;
    std::vector<std::string> truncatedSections;
    bool inventoryAvailable = false;
    bool inventoryTruncated = false;
    const std::string inventory = BuildInventory(
        player, inventoryTruncated, inventoryAvailable);
    if (!inventoryAvailable)
    {
        missing.push_back(JsonString("inventory"));
    }
    if (inventoryTruncated)
    {
        truncatedSections.push_back(JsonString("inventory"));
    }

    bool objectivesAvailable = false;
    bool objectivesTruncated = false;
    const std::string objectives = BuildObjectives(
        gameState, objectivesTruncated, objectivesAvailable);
    if (!objectivesAvailable)
    {
        missing.push_back(JsonString("objectives"));
    }
    if (objectivesTruncated)
    {
        truncatedSections.push_back(JsonString("objectives"));
    }

    bool environmentAvailable = false;
    const std::string environment = BuildEnvironment(world, gameState, environmentAvailable);
    if (!environmentAvailable)
    {
        missing.push_back(JsonString("environment"));
    }

    bool targetAvailable = false;
    bool targetTruncated = false;
    const std::string target = BuildTarget(world, player, targetAvailable, targetTruncated);
    if (!targetAvailable)
    {
        missing.push_back(JsonString("target"));
    }
    if (targetTruncated)
    {
        truncatedSections.push_back(JsonString("target"));
    }

    bool baseAvailable = false;
    const std::string base = BuildBase(gameState, baseAvailable);
    if (!baseAvailable)
    {
        missing.push_back(JsonString("base"));
    }

    bool sessionAvailable = false;
    const std::string session = BuildSession(gameState, sessionAvailable);
    if (!sessionAvailable)
    {
        missing.push_back(JsonString("session"));
    }

    bool progressionAvailable = false;
    bool progressionTruncated = false;
    const std::string progression = BuildProgression(
        gameState, controller, progressionAvailable, progressionTruncated);
    if (!progressionAvailable)
    {
        missing.push_back(JsonString("progression"));
    }
    if (progressionTruncated)
    {
        truncatedSections.push_back(JsonString("progression"));
    }

    const SDK::FCrCharacterPlayerSurvivalData survival = player->GetCharacterSurvivalData();
    Fields statusFields;
    Add(statusFields, "dead", JsonBoolean(survival.bDead));
    Add(statusFields, "incapacitated", JsonBoolean(player->IsIncapacitated()));
    Add(statusFields, "sprinting", JsonBoolean(player->IsSprinting()));
    Add(statusFields, "afk", JsonBoolean(player->IsAFK()));
    Add(statusFields, "interior", JsonBoolean(player->IsInInterior()));
    Add(statusFields, "safe_interior", JsonBoolean(player->IsInSafeInterior()));
    Add(statusFields, "protected", JsonBoolean(player->IsProtected()));
    Add(statusFields, "in_combat", JsonBoolean(player->bIsInCombatState));
    const std::string profession = TruncateUtf8(player->GetProfessionName().ToString());
    if (!profession.empty())
    {
        Add(statusFields, "profession", JsonString(profession));
    }

    const SDK::FVector location = player->K2_GetActorLocation();
    const SDK::FRotator rotation = player->K2_GetActorRotation();
    Fields positionFields;
    AddNumber(positionFields, "x", location.X);
    AddNumber(positionFields, "y", location.Y);
    AddNumber(positionFields, "z", location.Z);
    AddNumber(positionFields, "yaw", rotation.Yaw);

    Fields playerFields;
    Add(playerFields, "status", JsonObject(statusFields));
    Add(playerFields, "position", JsonObject(positionFields));
    Add(playerFields, "vitals", BuildVitals(player));
    Add(playerFields, "inventory", inventory);
    Add(playerFields, "equipment", BuildEquipment(player));

    Fields snapshotStatus;
    Add(snapshotStatus, "available", "true");
    Add(snapshotStatus, "partial", JsonBoolean(!missing.empty()));
    Add(snapshotStatus, "missing_sections", JsonArray(missing));
    Add(snapshotStatus, "truncated_sections", JsonArray(truncatedSections));

    Fields root;
    Add(root, "schema_version", "1");
    Add(root, "captured_at_unix_ms", JsonInteger(UnixTimeMilliseconds()));
    Fields sourceFields;
    Add(sourceFields, "kind", JsonString("client_observed"));
    Add(sourceFields, "game_sdk_build", JsonString("CL121391"));
    Add(sourceFields, "sample_interval_ms", JsonInteger(750));
    Add(root, "source", JsonObject(sourceFields));
    Add(root, "status", JsonObject(snapshotStatus));
    Add(root, "session", session);
    Add(root, "player", JsonObject(playerFields));
    Add(root, "progression", progression);
    Add(root, "objectives", objectives);
    Add(root, "environment", environment);
    Add(root, "target", target);
    Add(root, "base", base);
    std::string snapshot = JsonObject(root);
    if (snapshot.size() > MaxSnapshotBytes)
    {
        return BuildUnavailableSnapshot("snapshot_size_limit");
    }
    return snapshot;
}

void StoreSnapshot(std::string snapshot)
{
    std::scoped_lock lock(g_snapshotMutex);
    g_snapshot = std::move(snapshot);
}

void OnWorldBeginPlay(SDK::UWorld* world)
{
    g_world = world;
    g_sampleAccumulator = SampleIntervalSeconds;
    StoreSnapshot(BuildUnavailableSnapshot("waiting_for_local_player"));
}

void OnWorldEndPlay(SDK::UWorld* world, const char*)
{
    if (world == g_world)
    {
        g_world = nullptr;
        g_sampleAccumulator = SampleIntervalSeconds;
        StoreSnapshot(BuildUnavailableSnapshot("world_ended"));
    }
}

void OnEngineTick(const float deltaSeconds)
{
    if (g_world == nullptr)
    {
        return;
    }
    if (deltaSeconds > 0.0f && deltaSeconds <= 5.0f)
    {
        g_sampleAccumulator += deltaSeconds;
    }
    else
    {
        g_sampleAccumulator = SampleIntervalSeconds;
    }
    if (g_sampleAccumulator < SampleIntervalSeconds)
    {
        return;
    }
    g_sampleAccumulator = 0.0f;
    try
    {
        StoreSnapshot(Capture(g_world));
    }
    catch (...)
    {
        StoreSnapshot(BuildUnavailableSnapshot("capture_failed"));
        if (g_self != nullptr && g_self->logger != nullptr)
        {
            g_self->logger->Error(g_self, "%s", "Live game-state capture failed");
        }
    }
}
} // namespace

bool Initialize(IPluginSelf* self)
{
    if (g_registered)
    {
        return true;
    }
    g_self = self;
    StoreSnapshot(BuildUnavailableSnapshot("world_unavailable"));
    if (self == nullptr || self->hooks == nullptr || self->hooks->Engine == nullptr
        || self->hooks->World == nullptr)
    {
        return false;
    }
    self->hooks->Engine->RegisterOnTick(&OnEngineTick);
    self->hooks->World->RegisterOnWorldBeginPlay(&OnWorldBeginPlay);
    self->hooks->World->RegisterOnBeforeWorldEndPlay(&OnWorldEndPlay);
    g_registered = true;
    return true;
}

void Shutdown()
{
    if (g_registered && g_self != nullptr && g_self->hooks != nullptr)
    {
        if (g_self->hooks->Engine != nullptr)
        {
            g_self->hooks->Engine->UnregisterOnTick(&OnEngineTick);
        }
        if (g_self->hooks->World != nullptr)
        {
            g_self->hooks->World->UnregisterOnWorldBeginPlay(&OnWorldBeginPlay);
            g_self->hooks->World->UnregisterOnBeforeWorldEndPlay(&OnWorldEndPlay);
        }
    }
    g_registered = false;
    g_world = nullptr;
    g_self = nullptr;
    StoreSnapshot(BuildUnavailableSnapshot("plugin_stopped"));
}

std::string Snapshot()
{
    std::scoped_lock lock(g_snapshotMutex);
    return g_snapshot;
}
} // namespace RuptureCompanion::LiveContext
